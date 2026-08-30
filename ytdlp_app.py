#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ytdlp_app.py - single unified cross-platform app: local yt-dlp service +
tray icon + on-demand desktop window, all in one process and one file.

Same local server that answers the website (udl.moviora.win) also serves
this app's own UI - the desktop window is just another client of
http://127.0.0.1:PORT/, exactly like the browser is. One shared frontend
(index.html) for web and desktop.

Left click on tray icon      -> open/focus the app window (Download tab)
Win+Shift+D anywhere         -> same as left click (global hotkey; on macOS
                                 this is Cmd+Shift+D)
Right click on tray icon     -> "Open" / "Open log" / "Run with OS" / "Exit"
Closing the app window       -> just closes that window normally. The app
                                 keeps running in the tray regardless -
                                 opening it again creates a fresh window.
Exit in the tray menu        -> this is what actually stops everything.

IMPORTANT DESIGN NOTE - why windows are destroyed on close, not hidden:
pywebview has a reproducible bug where intercepting the window "closing"
event and calling window.hide() instead of letting it close FREEZES the
whole process (see https://github.com/r0x0r/pywebview/issues/1103 - this
was independently reproduced while building this app, not just cited from
the issue tracker). So instead of hide-on-close, we let windows close and
destroy normally, and keep the app alive via a permanent hidden "anchor"
window that is never shown or destroyed until Exit (webview.start() only
returns once ALL windows are destroyed, so the anchor is what keeps it
running with zero visible windows open).

Dependencies:
    pip install yt-dlp flask flask-cors pywebview pystray pillow pynput

FFmpeg must be available in PATH for merging video+audio, audio/image/video
conversion and subtitle embedding: https://ffmpeg.org/download.html

Run:
    python ytdlp_app.py

Build to .exe:
    pyinstaller --onefile --windowed --name ytdlp-app ytdlp_app.py
"""

# =============================================================================
# 1) IMPORTS + SETTINGS + PURE HELPER FUNCTIONS + Api CLASS (yt-dlp/ffmpeg engine)
# =============================================================================

import json
import locale
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import warnings
import webbrowser
from datetime import datetime
from urllib.parse import quote_plus
import urllib.request

from flask import Flask, jsonify, request
from flask_cors import CORS
from PIL import Image, ImageDraw
import pystray
import webview
from pynput import keyboard
from werkzeug.serving import make_server

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


APP_NAME = "ytdlp-gui"
BROWSERS = ["chrome", "firefox", "edge", "brave", "opera", "vivaldi", "chromium", "safari"]
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MAX_LOG_LINES = 400


# --------------------------------------------------------------------------- #
#  AppData / nastavení                                                        #
# --------------------------------------------------------------------------- #

def get_appdata_dir():
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


SETTINGS_FILE = os.path.join(get_appdata_dir(), "settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def detect_system_language():
    """Zjistí jazyk OS a namapuje ho na jeden z podporovaných (jinak 'en')."""
    supported = {
        "cs": ("cs", "cz"),
        "pl": ("pl",),
        "fr": ("fr",),
        "ja": ("ja",),
        "en": ("en",),
    }
    candidates = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            loc = locale.getdefaultlocale()[0]  # e.g. 'cs_CZ' (deprecated but still works)
            if loc:
                candidates.append(loc)
    except Exception:
        pass
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            candidates.append(val.split(":")[0])
    if platform.system() == "Windows":
        try:
            import ctypes
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            win_locale = locale.windows_locale.get(lcid)
            if win_locale:
                candidates.append(win_locale)
        except Exception:
            pass

    for candidate in candidates:
        candidate = candidate.lower().replace("-", "_")
        for code, prefixes in supported.items():
            if any(candidate.startswith(p) for p in prefixes):
                return code
    return "en"


def default_settings():
    return {
        "language": detect_system_language(),
        "appearance": "dark",
        "platform": "Auto-Detect",
        "mode": "video",
        "playlist": False,
        "cookie_mode": "none",
        "cookie_path": "",
        "browser": "chrome",
        "outdir": os.path.join(os.path.expanduser("~"), "Downloads"),
        "rename": "",
        "ratelimit": "",
        "last_url": "",
    }


def merged_settings():
    result = default_settings()
    loaded = load_settings()
    if loaded.get("language") == "cz":
        loaded["language"] = "cs"
    result.update({k: v for k, v in loaded.items() if k in result})
    return result


# --------------------------------------------------------------------------- #
#  Čisté pomocné funkce (snadno testovatelné bez GUI)                         #
# --------------------------------------------------------------------------- #

def sample_info(info):
    if info.get("entries"):
        for entry in info["entries"]:
            if entry:
                return entry
    return info


def safe_number(value, fallback=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def format_duration(seconds):
    if not seconds:
        return "?"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def sanitize_filename(name):
    name = name or ""
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    return name.strip() or "download"


def guess_ext_from_url(url):
    clean = url.lower().split("?", 1)[0]
    for extension in ("jpg", "jpeg", "png", "webp"):
        if clean.endswith("." + extension):
            return extension
    return "jpg"


def human_size(num_bytes):
    if not num_bytes:
        return None
    num_bytes = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def parse_rate_limit(text):
    text = (text or "").strip().upper()
    if not text:
        return None
    mult = 1
    if text.endswith("K"):
        mult, text = 1024, text[:-1]
    elif text.endswith("M"):
        mult, text = 1024 ** 2, text[:-1]
    elif text.endswith("G"):
        mult, text = 1024 ** 3, text[:-1]
    try:
        value = float(text) * mult
        return int(value) if value > 0 else None
    except ValueError:
        return None


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def get_thumbnails(info):
    thumbnails = [item for item in (info.get("thumbnails") or []) if item.get("url")]
    thumbnails.sort(key=lambda item: (item.get("width") or 0) * (item.get("height") or 0), reverse=True)
    return thumbnails


def serialize_info(info):
    """Keep only data needed by the browser; full yt-dlp info can be huge."""
    reference = sample_info(info)
    formats = []
    for item in reference.get("formats") or []:
        formats.append({
            "format_id": str(item.get("format_id") or ""),
            "ext": item.get("ext") or "",
            "height": item.get("height"),
            "width": item.get("width"),
            "vcodec": item.get("vcodec") or "none",
            "acodec": item.get("acodec") or "none",
            "language": item.get("language") or "default",
            "abr": item.get("abr"),
            "tbr": item.get("tbr"),
            "filesize": item.get("filesize") or item.get("filesize_approx"),
        })

    def subtitle_languages(key):
        return sorted((reference.get(key) or {}).keys())

    thumbnail_data = [
        {"url": t.get("url"), "id": str(t.get("id") or ""), "width": t.get("width"), "height": t.get("height")}
        for t in get_thumbnails(reference)
    ]

    entries = [entry for entry in (info.get("entries") or []) if entry]
    return {
        "title": reference.get("title") or "?",
        "uploader": reference.get("uploader") or reference.get("channel") or "?",
        "duration": format_duration(reference.get("duration")),
        "playlist_count": len(entries),
        "formats": formats,
        "subtitles": subtitle_languages("subtitles"),
        "automatic_captions": subtitle_languages("automatic_captions"),
        "thumbnails": thumbnail_data,
    }


def build_video_format(height, language):
    language = language or "default"
    lang_filter = f"[language={language}]" if language != "default" else ""
    if height:
        return f"bestvideo[height<={height}]+bestaudio{lang_filter}/best[height<={height}]"
    return f"bestvideo+bestaudio{lang_filter}/best"


def build_audio_format(format_id, playlist, language):
    if format_id and not playlist:
        return format_id
    language = language or "default"
    lang_filter = f"[language={language}]" if language != "default" else ""
    return f"bestaudio{lang_filter}/bestaudio"


def outtmpl_for(outdir, playlist, rename):
    base = sanitize_filename(rename) if rename else None
    if base:
        if playlist:
            return os.path.join(outdir, f"{base} - %(playlist_index)s.%(ext)s")
        return os.path.join(outdir, f"{base}.%(ext)s")
    if playlist:
        return os.path.join(outdir, "%(playlist_index)s - %(title)s.%(ext)s")
    return os.path.join(outdir, "%(title)s.%(ext)s")


# --------------------------------------------------------------------------- #
#  API vystavené do JavaScriptu                                               #
# --------------------------------------------------------------------------- #

class Api:
    def __init__(self):
        self.window = None
        self.busy = False
        self._lock = threading.Lock()
        self._gen = 0

        self._log_buffer = []
        self.progress = {"percent": 0, "text": ""}
        self.info = None
        self.info_rev = 0
        self.error = None
        self.error_rev = 0
        self.done_rev = 0
        self.done_message = ""

        self._settings = merged_settings()

    # -- napojení na okno ------------------------------------------------ #

    def set_window(self, window):
        self.window = window

    # -- interní pomocné metody ------------------------------------------ #

    def _log(self, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log_buffer.append(f"[{stamp}] {message}")
        if len(self._log_buffer) > MAX_LOG_LINES:
            self._log_buffer = self._log_buffer[-MAX_LOG_LINES:]

    def _set_error(self, title, exception=None):
        message = f"{title}: {exception}" if exception is not None else title
        self._log(message)
        self.error = message
        self.error_rev += 1

    def _set_done(self, message):
        self.done_message = message
        self.done_rev += 1
        self._log(message)

    def _try_start(self):
        """Vrátí True a nastaví busy=True, pokud zrovna nic neběží."""
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            self._gen += 1
            return True

    def _finish(self, gen):
        with self._lock:
            if gen == self._gen:
                self.busy = False

    # -- volané z JS: polling --------------------------------------------- #

    def poll(self):
        logs, self._log_buffer = self._log_buffer, []
        return {
            "log": logs,
            "progress": self.progress,
            "busy": self.busy,
            "info": self.info,
            "info_rev": self.info_rev,
            "error": self.error,
            "error_rev": self.error_rev,
            "done_rev": self.done_rev,
            "done_message": self.done_message,
            "ffmpeg_ok": ffmpeg_available(),
        }

    # -- nastavení --------------------------------------------------------- #

    def get_settings(self):
        return self._settings

    def save_settings(self, values):
        if isinstance(values, dict):
            allowed = set(default_settings())
            self._settings.update({k: v for k, v in values.items() if k in allowed})
            save_settings(self._settings)
        return {"ok": True}

    # -- dialogy / OS ------------------------------------------------------ #

    def select_path(self, kind):
        if not self.window:
            # V sjednocené appce se sem nemělo jak dostat - dialog jde otevřít
            # jen z UI, a UI běží uvnitř okna, které v tu chvíli musí existovat.
            self._log("Dialog error: no active window to attach the dialog to.")
            return ""
        try:
            if kind == "directory":
                result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            else:
                result = self.window.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False, file_types=("All files (*.*)",))
            return result[0] if result else ""
        except Exception as exc:
            self._log(f"Dialog error: {exc}")
            return ""

    def open_folder(self, path):
        path = os.path.abspath(path or "")
        if not os.path.isdir(path):
            return {"ok": False, "error": "Folder does not exist."}
        try:
            system = platform.system()
            if system == "Windows":
                os.startfile(path)  # noqa
            elif system == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_search(self, query):
        query = (query or "").strip()
        if query:
            webbrowser.open("https://www.google.com/search?q=" + quote_plus(query))
        return {"ok": True}

    # -- yt-dlp common opts -------------------------------------------------- #

    def _common_opts(self, config):
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": not bool(config.get("playlist")),
            "socket_timeout": 20,
            "retries": 3,
            "fragment_retries": 3,
        }
        cookie_mode = config.get("cookie_mode", "none")
        if cookie_mode == "file" and config.get("cookie_path"):
            options["cookiefile"] = config["cookie_path"]
        elif cookie_mode == "browser" and config.get("browser"):
            options["cookiesfrombrowser"] = (config["browser"],)

        platform_name = (config.get("platform") or "").lower()
        if "instagram" in platform_name:
            options["http_headers"] = {"Referer": "https://www.instagram.com/"}
        elif "tiktok" in platform_name:
            options["http_headers"] = {"Referer": "https://www.tiktok.com/"}

        rate = parse_rate_limit(config.get("ratelimit"))
        if rate:
            options["ratelimit"] = rate
        return options

    # -- fetch info ---------------------------------------------------------- #

    def fetch_info(self, url, config=None):
        url = (url or "").strip()
        config = config if isinstance(config, dict) else {}
        if not url:
            return {"ok": False, "error": "Vložte URL adresu."}
        if yt_dlp is None:
            return {"ok": False, "error": "Chybí modul yt-dlp. Nainstalujte: pip install yt-dlp"}
        if not self._try_start():
            return {"ok": False, "error": "Už právě něco běží, počkejte prosím."}
        gen = self._gen
        self._log(f"Načítám informace: {url}")
        threading.Thread(target=self._fetch_info_worker, args=(gen, url, config), daemon=True).start()
        return {"ok": True}

    def _fetch_info_worker(self, gen, url, config):
        try:
            options = self._common_opts(config)
            options.update({"skip_download": True, "extract_flat": False})
            with yt_dlp.YoutubeDL(options) as downloader:
                raw_info = downloader.extract_info(url, download=False)
            if gen != self._gen:
                return  # zastaralý požadavek, zahazujeme
            self.info = serialize_info(raw_info)
            self.info_rev += 1
            self._log("Informace byly načteny.")
        except Exception as exc:
            if gen == self._gen:
                self._set_error("Načtení informací selhalo", exc)
        finally:
            self._finish(gen)

    # -- download -------------------------------------------------------------- #

    def download(self, url, config=None):
        url = (url or "").strip()
        config = config if isinstance(config, dict) else {}
        if not url:
            return {"ok": False, "error": "Vložte URL adresu."}
        if yt_dlp is None:
            return {"ok": False, "error": "Chybí modul yt-dlp."}
        outdir = (config.get("outdir") or "").strip()
        if not outdir:
            return {"ok": False, "error": "Vyberte cílovou složku."}
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"Cílovou složku nelze vytvořit: {exc}"}
        if not self._try_start():
            return {"ok": False, "error": "Už právě něco běží, počkejte prosím."}

        self._settings.update({
            "last_url": url, "outdir": outdir,
            "mode": config.get("mode", "video"),
            "playlist": bool(config.get("playlist")),
            "rename": config.get("rename", ""),
            "ratelimit": config.get("ratelimit", ""),
        })
        save_settings(self._settings)

        gen = self._gen
        self.progress = {"percent": 0, "text": ""}
        threading.Thread(target=self._download_worker, args=(gen, url, config, outdir), daemon=True).start()
        return {"ok": True}

    def _progress_hook(self, data):
        status = data.get("status")
        if status == "downloading":
            percent = safe_number((data.get("_percent_str") or "0").replace("%", ""))
            self.progress = {
                "percent": percent,
                "text": "{}  {}  ETA {}".format(
                    (data.get("_percent_str") or "0%").strip(),
                    (data.get("_speed_str") or "").strip(),
                    (data.get("_eta_str") or "?").strip(),
                ),
            }
        elif status == "finished":
            self._log(f"Staženo: {os.path.basename(data.get('filename') or '')}")

    def _download_worker(self, gen, url, config, outdir):
        try:
            mode = config.get("mode", "video")
            playlist = bool(config.get("playlist"))
            if mode == "video":
                self._download_video(url, config, outdir, playlist)
            elif mode == "audio":
                self._download_audio(url, config, outdir, playlist)
            elif mode == "subs":
                self._download_subtitles(url, config, outdir, playlist)
            elif mode == "thumb":
                self._download_thumbnails(url, config, outdir)
            else:
                raise RuntimeError(f"Neznámý mód: {mode}")
            if gen == self._gen:
                self._set_done("Stahování bylo úspěšně dokončeno.")
        except Exception as exc:
            if gen == self._gen:
                self._set_error("Stahování selhalo", exc)
        finally:
            self._finish(gen)

    def _download_video(self, url, config, outdir, playlist):
        height = config.get("video_height")
        try:
            height = int(height) if height else None
        except (TypeError, ValueError):
            height = None
        language = config.get("video_language") or "default"
        fmt = build_video_format(height, language)

        container = (config.get("video_container") or "auto").lower()
        options = self._common_opts(config)
        options.update({
            "format": fmt,
            "outtmpl": outtmpl_for(outdir, playlist, config.get("rename")),
            "merge_output_format": container if container != "auto" else "mp4",
            "progress_hooks": [self._progress_hook],
        })
        postprocessors = []
        if container != "auto":
            if not ffmpeg_available():
                raise RuntimeError("ffmpeg nebyl nalezen v PATH - je potřeba pro převod kontejneru.")
            postprocessors.append({"key": "FFmpegVideoConvertor", "preferedformat": container})

        has_manual = bool(config.get("has_manual_subs"))
        has_auto = bool(config.get("has_auto_subs"))
        if config.get("embed_subs") and (has_manual or has_auto):
            if not ffmpeg_available():
                raise RuntimeError("ffmpeg nebyl nalezen v PATH - je potřeba pro vložení titulků.")
            options["writesubtitles"] = has_manual
            options["writeautomaticsub"] = (not has_manual) and has_auto
            options["subtitleslangs"] = ["all"]
            postprocessors.append({"key": "FFmpegEmbedSubtitle"})
        if postprocessors:
            options["postprocessors"] = postprocessors

        self._log(f"Stahuji video (formát: {fmt})...")
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])

    def _download_audio(self, url, config, outdir, playlist):
        format_id = config.get("audio_format_id")
        language = config.get("audio_language") or "default"
        fmt = build_audio_format(format_id, playlist, language)

        options = self._common_opts(config)
        options.update({
            "format": fmt,
            "outtmpl": outtmpl_for(outdir, playlist, config.get("rename")),
            "progress_hooks": [self._progress_hook],
        })
        convert = (config.get("audio_convert") or "none").lower()
        if convert != "none":
            if not ffmpeg_available():
                raise RuntimeError("ffmpeg nebyl nalezen v PATH - je potřeba pro převod audia.")
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio", "preferredcodec": convert, "preferredquality": "192",
            }]
        self._log(f"Stahuji audio (formát: {fmt})...")
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])

    def _download_subtitles(self, url, config, outdir, playlist):
        language = config.get("subtitle_language")
        if not language:
            raise RuntimeError("Pro toto video nejsou k dispozici titulky.")
        source = config.get("subtitle_source", "original")
        options = self._common_opts(config)
        options.update({
            "skip_download": True,
            "writesubtitles": source == "original",
            "writeautomaticsub": source == "automatic",
            "subtitleslangs": [language],
            "subtitlesformat": config.get("subtitle_format") or "best",
            "outtmpl": outtmpl_for(outdir, playlist, config.get("rename")),
            "progress_hooks": [self._progress_hook],
        })
        self._log(f"Stahuji titulky ({language})...")
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])

    def _download_thumbnails(self, url, config, outdir):
        options = self._common_opts(config)
        options["skip_download"] = True
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
        entries = [entry for entry in (info.get("entries") or []) if entry] or [info]
        selected_index = int(config.get("thumbnail_index") or 0)
        convert = (config.get("thumbnail_convert") or "none").lower()
        rename = config.get("rename")

        for entry in entries:
            thumbnails = get_thumbnails(entry)
            if not thumbnails:
                continue
            selected = thumbnails[min(selected_index, len(thumbnails) - 1)]
            ext = guess_ext_from_url(selected["url"])
            base = sanitize_filename(rename) if rename else sanitize_filename(entry.get("title") or "thumbnail")
            filename = os.path.join(outdir, f"{base}_nahled.{ext}")
            self._log(f"Stahuji náhled: {os.path.basename(filename)}")
            urllib.request.urlretrieve(selected["url"], filename)
            if convert != "none" and convert != ext:
                if not ffmpeg_available():
                    self._log("ffmpeg nenalezen - přeskakuji převod náhledu.")
                    continue
                filename = self._ffmpeg_convert(filename, convert)

    def _ffmpeg_convert(self, source, target_ext):
        output = os.path.splitext(source)[0] + "." + target_ext
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", source, output], check=True)
            if os.path.exists(source) and source != output:
                os.remove(source)
            return output
        except Exception as exc:
            self._log(f"Převod obrázku selhal: {exc}")
            return source

    # -- obecný FFmpeg konvertor (nástroje) ------------------------------------- #

    def convert_file(self, source, target):
        source = (source or "").strip()
        target = (target or "mp3").strip().lower()
        if not os.path.isfile(source):
            return {"ok": False, "error": "Vyberte platný zdrojový soubor."}
        if not ffmpeg_available():
            return {"ok": False, "error": "ffmpeg nebyl nalezen v PATH."}
        if not self._try_start():
            return {"ok": False, "error": "Už právě něco běží, počkejte prosím."}
        gen = self._gen
        output = os.path.splitext(source)[0] + "_converted." + target
        threading.Thread(target=self._convert_worker, args=(gen, source, output), daemon=True).start()
        return {"ok": True}

    def _convert_worker(self, gen, source, output):
        try:
            self._log(f"FFmpeg: {os.path.basename(source)} → {os.path.basename(output)}")
            process = subprocess.Popen(
                ["ffmpeg", "-y", "-i", source, output],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, encoding="utf-8", errors="replace",
            )
            if process.stdout:
                for line in process.stdout:
                    line = line.strip()
                    if line:
                        self._log(line)
            process.wait()
            if process.returncode != 0:
                raise RuntimeError(f"FFmpeg skončilo s kódem {process.returncode}.")
            if gen == self._gen:
                self._set_done("Konverze byla úspěšně dokončena.")
        except Exception as exc:
            if gen == self._gen:
                self._set_error("Konverze selhala", exc)
        finally:
            self._finish(gen)

    # -- update yt-dlp ------------------------------------------------------------ #

    def update_ytdlp(self):
        if not self._try_start():
            return {"ok": False, "error": "Už právě něco běží, počkejte prosím."}
        gen = self._gen
        threading.Thread(target=self._update_worker, args=(gen,), daemon=True).start()
        return {"ok": True}

    def _update_worker(self, gen):
        try:
            if getattr(sys, "frozen", False):
                raise RuntimeError(
                    "Tohle je samostatně zabalená aplikace (.exe) - yt-dlp je v ní zamrzlý jako "
                    "součást programu a nejde takto aktualizovat. Stáhni si novější verzi aplikace, "
                    "jakmile vyjde. (Pokud appku spouštíš přímo přes 'python ytdlp_gui.py' ze zdrojáku, "
                    "tlačítko funguje normálně.)"
                )
            self._log("Aktualizuji yt-dlp...")
            result = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                                     capture_output=True, text=True)
            if result.stdout:
                self._log(result.stdout.strip())
            if result.stderr:
                self._log(result.stderr.strip())
            if result.returncode != 0:
                raise RuntimeError(f"pip skončil s kódem {result.returncode}, viz log výše.")
            if gen == self._gen:
                self._set_done("yt-dlp byl aktualizován. Restartuj aplikaci.")
        except Exception as exc:
            if gen == self._gen:
                self._set_error("Aktualizace selhala", exc)
        finally:
            self._finish(gen)

    def close(self):
        if self.window:
            self.window.destroy()
        return {"ok": True}

# =============================================================================
# 2) LOCAL HTTP SERVER (Flask) - exposes the Api class over HTTP, serves index.html
# =============================================================================

# Poslouchá JEN na 127.0.0.1 (nikdy ne na 0.0.0.0), takže není dostupná z jiných
# počítačů v síti - jen z prohlížeče na téhle stanici. Statická stránka na
# udl.moviora.win na ni volá přes fetch() a získává tak plnou funkčnost
# yt-dlp, aniž by uživatel musel cokoliv jiného instalovat.

PORT = int(os.environ.get("YTDLP_LOCAL_PORT", "47831"))
HOST = "127.0.0.1"  # NIKDY neměnit na 0.0.0.0 - jen lokální přístup!

# Sem patří doména(y), odkud appku hostuješ + localhost pro vývoj. Wildcard
# "*" záměrně nepoužívej - jakákoliv otevřená stránka v prohlížeči by pak
# mohla tuhle lokální službu volat a spouštět stahování / číst složky na disku.
ALLOWED_ORIGINS = [
    "https://udl.moviora.win",
    "http://localhost:5500",   # např. VS Code Live Server při vývoji stránky
    "http://127.0.0.1:5500",
]

VERSION = "1.0.0"

app = Flask(__name__)
CORS(app, origins=ALLOWED_ORIGINS)

api = Api()

# Cesta k index.html - stejný soubor, co je hostovaný na udl.moviora.win, teď
# ho obsluhujeme i lokálně, aby ho mohlo natáhnout i vlastní desktopové okno
# appky (ytdlp_app.py) - jedno UI pro web i desktop.
_INDEX_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
try:
    with open(_INDEX_HTML_PATH, "r", encoding="utf-8") as _handle:
        _INDEX_HTML = _handle.read()
except OSError:
    _INDEX_HTML = "<h1>index.html nebyl nalezen vedle ytdlp_local_server.py</h1>"


# --------------------------------------------------------------------------- #
#  Pomocné funkce pro parsování požadavků                                     #
# --------------------------------------------------------------------------- #

def json_body():
    return request.get_json(silent=True) or {}


# --------------------------------------------------------------------------- #
#  Statická stránka (pro desktopové okno appky - viz ytdlp_app.py)            #
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return _INDEX_HTML


# --------------------------------------------------------------------------- #
#  Endpointy                                                                  #
# --------------------------------------------------------------------------- #

@app.get("/api/ping")
def ping():
    """Tohle stránka volá jako první - zjišťuje, jestli služba běží."""
    return jsonify({
        "ok": True,
        "version": VERSION,
        "yt_dlp_available": yt_dlp is not None,
        "ffmpeg_ok": api.poll()["ffmpeg_ok"],
    })


@app.get("/api/settings")
def get_settings():
    return jsonify(api.get_settings())


@app.post("/api/settings")
def save_settings():
    return jsonify(api.save_settings(json_body()))


@app.post("/api/select_path")
def select_path():
    kind = json_body().get("kind", "file")
    path = api.select_path(kind)
    return jsonify({"path": path})


@app.post("/api/open_folder")
def open_folder():
    return jsonify(api.open_folder(json_body().get("path", "")))


@app.post("/api/open_search")
def open_search():
    return jsonify(api.open_search(json_body().get("query", "")))


@app.post("/api/fetch_info")
def fetch_info():
    body = json_body()
    return jsonify(api.fetch_info(body.get("url", ""), body.get("config", {})))


@app.post("/api/download")
def download():
    body = json_body()
    return jsonify(api.download(body.get("url", ""), body.get("config", {})))


@app.get("/api/poll")
def poll():
    return jsonify(api.poll())


@app.post("/api/convert_file")
def convert_file():
    body = json_body()
    return jsonify(api.convert_file(body.get("source", ""), body.get("target", "mp3")))


@app.post("/api/update_ytdlp")
def update_ytdlp():
    return jsonify(api.update_ytdlp())

# =============================================================================
# 3) DESKTOP SHELL - autostart, tray icon, on-demand window, global hotkey
# =============================================================================

APP_TITLE = "yt-dlp"
RUN_KEY_NAME = "YtDlpApp"
BUNDLE_ID = "win.moviora.ytdlp-app"
HOTKEY = "<cmd>+<shift>+d"  # Win+Shift+D on Windows/Linux, Cmd+Shift+D on macOS

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"
IS_MACOS = SYSTEM == "Darwin"
IS_LINUX = SYSTEM == "Linux"


# --------------------------------------------------------------------------- #
#  Start at login - identical cross-platform logic used across this project  #
# --------------------------------------------------------------------------- #

def _startup_command_list():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def _startup_command_quoted():
    return " ".join(f'"{part}"' for part in _startup_command_list())


def _macos_launch_agent_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{BUNDLE_ID}.plist")


def _linux_autostart_path():
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(xdg_config, "autostart", "ytdlp-app.desktop")


def is_autostart_enabled():
    if IS_WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, RUN_KEY_NAME)
                return True
        except FileNotFoundError:
            return False
    if IS_MACOS:
        return os.path.isfile(_macos_launch_agent_path())
    if IS_LINUX:
        return os.path.isfile(_linux_autostart_path())
    return False


def set_autostart(enabled):
    if IS_WINDOWS:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_KEY_NAME, 0, winreg.REG_SZ, _startup_command_quoted())
            else:
                try:
                    winreg.DeleteValue(key, RUN_KEY_NAME)
                except FileNotFoundError:
                    pass
        return

    if IS_MACOS:
        path = _macos_launch_agent_path()
        if enabled:
            import plistlib
            os.makedirs(os.path.dirname(path), exist_ok=True)
            plist = {
                "Label": BUNDLE_ID,
                "ProgramArguments": _startup_command_list(),
                "RunAtLoad": True,
                "KeepAlive": False,
            }
            with open(path, "wb") as handle:
                plistlib.dump(plist, handle)
            subprocess.run(["launchctl", "load", "-w", path], check=False)
        else:
            if os.path.isfile(path):
                subprocess.run(["launchctl", "unload", "-w", path], check=False)
                os.remove(path)
        return

    if IS_LINUX:
        path = _linux_autostart_path()
        if enabled:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            exec_line = " ".join(_startup_command_list())
            desktop_entry = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={APP_TITLE}\n"
                f"Exec={exec_line}\n"
                "X-GNOME-Autostart-enabled=true\n"
                "NoDisplay=false\n"
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(desktop_entry)
        else:
            if os.path.isfile(path):
                os.remove(path)
        return


# --------------------------------------------------------------------------- #
#  Flask server on a thread, with a real shutdown()                          #
# --------------------------------------------------------------------------- #

class ServerThread(threading.Thread):
    def __init__(self, flask_app, host, port):
        super().__init__(daemon=True)
        self.srv = make_server(host, port, flask_app)

    def run(self):
        self.srv.serve_forever()

    def shutdown(self):
        self.srv.shutdown()


# --------------------------------------------------------------------------- #
#  Window manager: one on-demand window, created fresh each time it's opened #
#  after being closed (see design note at the top of this file for why).     #
# --------------------------------------------------------------------------- #

class WindowManager:
    def __init__(self, base_url, api):
        self.base_url = base_url
        self.api = api
        self._window = None
        self._lock = threading.Lock()

    def open(self, tab=None):
        with self._lock:
            if self._window is not None:
                try:
                    self._window.show()
                    self._window.restore()
                    if tab:
                        self._window.evaluate_js(f'switchTab("{tab}")')
                    return self._window
                except Exception:
                    self._window = None  # window object is stale, fall through and recreate

            path = f"/?tab={tab}" if tab else "/"
            window = webview.create_window(
                APP_TITLE, url=self.base_url + path,
                width=1100, height=800, min_size=(880, 640),
            )
            window.events.closed += self._on_closed
            self._window = window
            self.api.set_window(window)
            return window

    def _on_closed(self):
        with self._lock:
            self._window = None
            self.api.set_window(None)

    def close(self):
        """Zavře aktuálně otevřené okno appky (pokud nějaké je) - volané při Exit,
        aby po zničení kotevního okna nezbylo viset ještě tohle a webview.start()
        se korektně vrátilo."""
        with self._lock:
            window, self._window = self._window, None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  Tray icon                                                                  #
# --------------------------------------------------------------------------- #

def make_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=(79, 70, 229, 255))
    draw.polygon([(36, 10), (20, 36), (30, 36), (26, 54), (46, 28), (35, 28)], fill=(255, 255, 255, 255))
    return img


def build_tray(window_manager, server_thread, hotkey_listener, anchor_window):
    def on_open(icon, item):
        threading.Thread(target=window_manager.open, daemon=True).start()

    def on_open_log(icon, item):
        threading.Thread(target=window_manager.open, kwargs={"tab": "logs"}, daemon=True).start()

    def on_toggle_autostart(icon, item):
        set_autostart(not is_autostart_enabled())

    def on_exit(icon, item):
        server_thread.shutdown()
        try:
            hotkey_listener.stop()
        except Exception:
            pass
        icon.stop()
        window_manager.close()  # zavřít případně otevřené okno appky, ať webview.start() nečeká na něj
        try:
            anchor_window.destroy()  # last window destroyed -> webview.start() returns -> app exits
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("Open", on_open, default=True),
        pystray.MenuItem("Open log", on_open_log),
        pystray.MenuItem("Run with OS", on_toggle_autostart, checked=lambda item: is_autostart_enabled()),
        pystray.MenuItem("Exit", on_exit),
    )
    return pystray.Icon("ytdlp-app", make_icon_image(), APP_TITLE, menu)


def main():
    server_thread = ServerThread(app, HOST, PORT)
    server_thread.start()

    base_url = f"http://{HOST}:{PORT}"
    window_manager = WindowManager(base_url, api)

    # Permanent hidden anchor window - keeps webview.start() alive even when
    # the visible app window has been closed (see design note above).
    anchor = webview.create_window(f"{APP_TITLE} (background)", html="<html></html>", hidden=True)

    hotkey_listener = keyboard.GlobalHotKeys({
        HOTKEY: lambda: threading.Thread(target=window_manager.open, daemon=True).start(),
    })
    hotkey_listener.start()

    icon = build_tray(window_manager, server_thread, hotkey_listener, anchor)
    threading.Thread(target=icon.run, daemon=True).start()

    print(f"{APP_TITLE} started. Server at {base_url}")
    webview.start()  # blocks until every window (including the anchor) is destroyed


if __name__ == "__main__":
    sys.exit(main() or 0)
