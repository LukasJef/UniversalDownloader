#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py - UniversalDownloader engine running directly inside Termux (a
real yt-dlp + a real ffmpeg on PATH, no Chaquopy, no ffmpeg-kit).

This is almost verbatim the same as the engine + Flask server part of the
desktop ytdlp_app.py - just without the desktop-only parts (pywebview,
pystray, autostart, hotkey) and with paths adapted for the Termux
environment.

Started in the background by the Android app (UniversalDownloader) via a
Termux RUN_COMMAND intent - see the android/ folder in this repo. The app
then simply opens a WebView at http://127.0.0.1:47831/, exactly like the
desktop window does.

Manual run for testing:
    python3 server.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from urllib.parse import quote_plus
import urllib.request

from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.serving import make_server

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


APP_NAME = "UniversalDownloader"
BROWSERS = ["chrome", "firefox", "edge", "brave", "opera", "vivaldi", "chromium", "safari"]
MAX_LOG_LINES = 400
MAX_CONSOLE_CHARS = 300_000

HOST = "127.0.0.1"
PORT = int(os.environ.get("YTDLP_LOCAL_PORT", "47831"))
VERSION = "1.0.1"

# Same domain the desktop/web version uses - the Termux server only ever
# answers on 127.0.0.1, so CORS here really only matters if someone opens
# udl.moviora.win directly in a mobile browser with Termux running.
ALLOWED_ORIGINS = [
    "https://udl.moviora.win",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]

HOME = os.path.expanduser("~")
UDL_DIR = os.path.join(HOME, "udl")
SETTINGS_FILE = os.path.join(UDL_DIR, "settings.json")
# ~/storage/downloads only exists after running "termux-setup-storage" (see
# README) - it's a symlink to the real Download folder that's visible to
# other apps too (gallery, file manager...), not just inside Termux.
DEFAULT_OUTDIR = os.path.join(HOME, "storage", "downloads")


# --------------------------------------------------------------------------- #
#  Capture stdout/stderr for the hidden /console page - same idea as on the  #
#  desktop, useful for debugging without adb/logcat.                        #
# --------------------------------------------------------------------------- #

_console_buffer = []
_console_lock = threading.Lock()


class _ConsoleCapture:
    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, text):
        if self.original_stream:
            try:
                self.original_stream.write(text)
            except Exception:
                pass
        if text:
            with _console_lock:
                _console_buffer.append(text)
                total = sum(len(chunk) for chunk in _console_buffer)
                while total > MAX_CONSOLE_CHARS and len(_console_buffer) > 1:
                    total -= len(_console_buffer.pop(0))

    def flush(self):
        if self.original_stream:
            try:
                self.original_stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def get_console_text():
    with _console_lock:
        return "".join(_console_buffer)


sys.stdout = _ConsoleCapture(sys.stdout)
sys.stderr = _ConsoleCapture(sys.stderr)


# --------------------------------------------------------------------------- #
#  Settings                                                                   #
# --------------------------------------------------------------------------- #

def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        os.makedirs(UDL_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def default_settings():
    return {
        "language": "en",
        "appearance": "dark",
        "platform": "Auto-Detect",
        "mode": "video",
        "playlist": False,
        "cookie_mode": "none",
        "cookie_path": "",
        "browser": "chrome",
        "outdir": DEFAULT_OUTDIR,
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
#  Pure helper functions (identical to the desktop version)                   #
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


# ISO 639-1 (2 letters, the format yt-dlp/YouTube uses) -> ISO 639-2/B
# (3 letters, required by MP4/MOV containers for metadata:s:a language tags).
ISO_639_1_TO_2 = {
    "aa": "aar", "ab": "abk", "af": "afr", "ak": "aka", "sq": "alb", "am": "amh",
    "ar": "ara", "an": "arg", "hy": "arm", "as": "asm", "av": "ava", "ae": "ave",
    "ay": "aym", "az": "aze", "ba": "bak", "bm": "bam", "eu": "baq", "be": "bel",
    "bn": "ben", "bh": "bih", "bi": "bis", "bs": "bos", "br": "bre", "bg": "bul",
    "my": "bur", "ca": "cat", "ch": "cha", "ce": "che", "zh": "chi", "cu": "chu",
    "cv": "chv", "kw": "cor", "co": "cos", "cr": "cre", "cs": "cze", "da": "dan",
    "dv": "div", "nl": "dut", "dz": "dzo", "en": "eng", "eo": "epo", "et": "est",
    "ee": "ewe", "fo": "fao", "fj": "fij", "fi": "fin", "fr": "fre", "fy": "fry",
    "ff": "ful", "ka": "geo", "de": "ger", "gd": "gla", "ga": "gle", "gl": "glg",
    "gv": "glv", "el": "gre", "gn": "grn", "gu": "guj", "ht": "hat", "ha": "hau",
    "he": "heb", "hz": "her", "hi": "hin", "ho": "hmo", "hr": "hrv", "hu": "hun",
    "ig": "ibo", "is": "ice", "io": "ido", "ii": "iii", "iu": "iku", "ie": "ile",
    "ia": "ina", "id": "ind", "ik": "ipk", "it": "ita", "jv": "jav", "ja": "jpn",
    "kl": "kal", "kn": "kan", "ks": "kas", "kr": "kau", "kk": "kaz", "km": "khm",
    "ki": "kik", "rw": "kin", "ky": "kir", "kv": "kom", "kg": "kon", "ko": "kor",
    "kj": "kua", "ku": "kur", "lo": "lao", "la": "lat", "lv": "lav", "li": "lim",
    "ln": "lin", "lt": "lit", "lb": "ltz", "lu": "lub", "lg": "lug", "mk": "mac",
    "mh": "mah", "ml": "mal", "mi": "mao", "mr": "mar", "ms": "may", "mg": "mlg",
    "mt": "mlt", "mn": "mon", "na": "nau", "nv": "nav", "nr": "nbl", "nd": "nde",
    "ng": "ndo", "ne": "nep", "nn": "nno", "nb": "nob", "no": "nor", "ny": "nya",
    "oc": "oci", "oj": "oji", "or": "ori", "om": "orm", "os": "oss", "pa": "pan",
    "fa": "per", "pi": "pli", "pl": "pol", "pt": "por", "ps": "pus", "qu": "que",
    "rm": "roh", "ro": "rum", "rn": "run", "ru": "rus", "sg": "sag", "sa": "san",
    "si": "sin", "sk": "slo", "sl": "slv", "se": "sme", "sm": "smo", "sn": "sna",
    "sd": "snd", "so": "som", "st": "sot", "es": "spa", "sc": "srd", "sr": "srp",
    "ss": "ssw", "su": "sun", "sw": "swa", "sv": "swe", "ty": "tah", "ta": "tam",
    "tt": "tat", "te": "tel", "tg": "tgk", "tl": "tgl", "th": "tha", "ti": "tir",
    "to": "ton", "tn": "tsn", "ts": "tso", "tk": "tuk", "tr": "tur", "tw": "twi",
    "ug": "uig", "uk": "ukr", "ur": "urd", "uz": "uzb", "ve": "ven", "vi": "vie",
    "vo": "vol", "wa": "wln", "wo": "wol", "xh": "xho", "yi": "yid", "yo": "yor",
    "za": "zha", "zu": "zul",
}


def to_iso639_2(language_code):
    if not language_code or language_code == "default":
        return None
    base = language_code.split("-")[0].lower()
    return ISO_639_1_TO_2.get(base, language_code)


def build_video_format(height, language, audio_format_ids=None):
    language = language or "default"
    if language == "__all__":
        if audio_format_ids:
            audio_part = "+".join(audio_format_ids)
            if height:
                return f"bestvideo[height<={height}]+{audio_part}/best[height<={height}]"
            return f"bestvideo+{audio_part}/best"
        if height:
            return f"bestvideo[height<={height}]+mergeall[acodec!=none]/best[height<={height}]"
        return "bestvideo+mergeall[acodec!=none]/best"
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
#  API exposed to JavaScript (index.html)                                     #
# --------------------------------------------------------------------------- #

class Api:
    def __init__(self):
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

    def get_settings(self):
        return self._settings

    def save_settings(self, values):
        if isinstance(values, dict):
            allowed = set(default_settings())
            self._settings.update({k: v for k, v in values.items() if k in allowed})
            save_settings(self._settings)
        return {"ok": True}

    # -- dialogs / OS (Termux has no native file picker without extra work) #

    def select_path(self, kind):
        if kind == "directory":
            return DEFAULT_OUTDIR
        self._log("File picking isn't supported yet in the Termux version.")
        return ""

    def open_folder(self, path):
        try:
            subprocess.run(["termux-open", path], check=True, capture_output=True)
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "Opening a folder requires the Termux:API package "
                                           "(pkg install termux-api + the Termux:API app from F-Droid)."}

    def open_search(self, query):
        query = (query or "").strip()
        if not query:
            return {"ok": True}
        try:
            subprocess.run(
                ["termux-open-url", "https://www.google.com/search?q=" + quote_plus(query)],
                check=True, capture_output=True,
            )
        except Exception as exc:
            self._log(f"Opening the browser failed (requires Termux:API): {exc}")
        return {"ok": True}

    # -- yt-dlp common opts ----------------------------------------------- #

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

    def fetch_info(self, url, config=None):
        url = (url or "").strip()
        config = config if isinstance(config, dict) else {}
        if not url:
            return {"ok": False, "error": "Please enter a URL."}
        if yt_dlp is None:
            return {"ok": False, "error": "The yt-dlp module is missing."}
        if not self._try_start():
            return {"ok": False, "error": "Something is already running, please wait."}
        gen = self._gen
        self._log(f"Fetching info: {url}")
        threading.Thread(target=self._fetch_info_worker, args=(gen, url, config), daemon=True).start()
        return {"ok": True}

    def _fetch_info_worker(self, gen, url, config):
        try:
            options = self._common_opts(config)
            options.update({"skip_download": True, "extract_flat": False})
            with yt_dlp.YoutubeDL(options) as downloader:
                raw_info = downloader.extract_info(url, download=False)
            if gen != self._gen:
                return
            self.info = serialize_info(raw_info)
            self.info_rev += 1
            self._log("Info fetched.")
        except Exception as exc:
            if gen == self._gen:
                self._set_error("Fetching info failed", exc)
        finally:
            self._finish(gen)

    def download(self, url, config=None):
        url = (url or "").strip()
        config = config if isinstance(config, dict) else {}
        if not url:
            return {"ok": False, "error": "Please enter a URL."}
        if yt_dlp is None:
            return {"ok": False, "error": "The yt-dlp module is missing."}
        outdir = (config.get("outdir") or "").strip()
        if not outdir:
            return {"ok": False, "error": "Please choose a destination folder."}
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"Couldn't create the destination folder: {exc}"}
        if not self._try_start():
            return {"ok": False, "error": "Something is already running, please wait."}

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
            self._log(f"Downloaded: {os.path.basename(data.get('filename') or '')}")

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
                raise RuntimeError(f"Unknown mode: {mode}")
            if gen == self._gen:
                self._set_done("Download completed successfully.")
        except Exception as exc:
            if gen == self._gen:
                self._set_error("Download failed", exc)
        finally:
            self._finish(gen)

    def _download_video(self, url, config, outdir, playlist):
        height = config.get("video_height")
        try:
            height = int(height) if height else None
        except (TypeError, ValueError):
            height = None
        language = config.get("video_language") or "default"
        audio_format_ids = config.get("video_audio_format_ids") or []
        fmt = build_video_format(height, language, audio_format_ids)

        container = (config.get("video_container") or "auto").lower()
        options = self._common_opts(config)
        options.update({
            "format": fmt,
            "outtmpl": outtmpl_for(outdir, playlist, config.get("rename")),
            "merge_output_format": container if container != "auto" else "mp4",
            "progress_hooks": [self._progress_hook],
        })
        if language == "__all__":
            options["allow_multiple_audio_streams"] = True
        postprocessors = []
        if container != "auto":
            if not ffmpeg_available():
                raise RuntimeError("ffmpeg wasn't found - it's needed to convert the container.")
            postprocessors.append({"key": "FFmpegVideoConvertor", "preferedformat": container})

        has_manual = bool(config.get("has_manual_subs"))
        has_auto = bool(config.get("has_auto_subs"))
        if config.get("embed_subs") and (has_manual or has_auto):
            if not ffmpeg_available():
                raise RuntimeError("ffmpeg wasn't found - it's needed to embed subtitles.")
            options["writesubtitles"] = has_manual
            options["writeautomaticsub"] = (not has_manual) and has_auto
            options["subtitleslangs"] = ["all"]
            postprocessors.append({"key": "FFmpegEmbedSubtitle"})
        if postprocessors:
            options["postprocessors"] = postprocessors

        self._log(f"Downloading video (format: {fmt})...")
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)

        if language == "__all__":
            languages = config.get("video_audio_languages") or []
            if languages and info:
                downloads = info.get("requested_downloads") or ([info] if info.get("filepath") else [])
                for item in downloads:
                    filepath = item.get("filepath")
                    if filepath and os.path.isfile(filepath):
                        self._tag_audio_languages(filepath, languages)

    def _tag_audio_languages(self, filepath, languages):
        if not ffmpeg_available():
            return
        tmp_path = filepath + ".tmp" + os.path.splitext(filepath)[1]
        cmd = ["ffmpeg", "-y", "-i", filepath, "-map", "0", "-c", "copy"]
        for i, lang in enumerate(languages):
            iso_lang = to_iso639_2(lang) or lang
            cmd += [f"-metadata:s:a:{i}", f"language={iso_lang}"]
        cmd.append(tmp_path)
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            os.replace(tmp_path, filepath)
            self._log("Audio track languages have been tagged in the file.")
        except Exception as exc:
            self._log(f"Tagging track languages failed (the file is fine, just without metadata): {exc}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

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
                raise RuntimeError("ffmpeg wasn't found - it's needed to convert audio.")
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio", "preferredcodec": convert, "preferredquality": "192",
            }]
        self._log(f"Downloading audio (format: {fmt})...")
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])

    def _download_subtitles(self, url, config, outdir, playlist):
        language = config.get("subtitle_language")
        if not language:
            raise RuntimeError("No subtitles are available for this video.")
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
        self._log(f"Downloading subtitles ({language})...")
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
            filename = os.path.join(outdir, f"{base}_thumbnail.{ext}")
            self._log(f"Downloading thumbnail: {os.path.basename(filename)}")
            urllib.request.urlretrieve(selected["url"], filename)
            if convert != "none" and convert != ext:
                if not ffmpeg_available():
                    self._log("ffmpeg not found - skipping thumbnail conversion.")
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
            self._log(f"Image conversion failed: {exc}")
            return source

    def convert_file(self, source, target):
        source = (source or "").strip()
        target = (target or "mp3").strip().lower()
        if not os.path.isfile(source):
            return {"ok": False, "error": "Please choose a valid source file."}
        if not ffmpeg_available():
            return {"ok": False, "error": "ffmpeg wasn't found."}
        if not self._try_start():
            return {"ok": False, "error": "Something is already running, please wait."}
        gen = self._gen
        output = os.path.splitext(source)[0] + "_converted." + target
        threading.Thread(target=self._convert_worker, args=(gen, source, output), daemon=True).start()
        return {"ok": True}

    def _convert_worker(self, gen, source, output):
        try:
            self._log(f"FFmpeg: {os.path.basename(source)} -> {os.path.basename(output)}")
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
                raise RuntimeError(f"FFmpeg exited with code {process.returncode}.")
            if gen == self._gen:
                self._set_done("Conversion completed successfully.")
        except Exception as exc:
            if gen == self._gen:
                self._set_error("Conversion failed", exc)
        finally:
            self._finish(gen)

    def update_ytdlp(self):
        if not self._try_start():
            return {"ok": False, "error": "Something is already running, please wait."}
        gen = self._gen
        threading.Thread(target=self._update_worker, args=(gen,), daemon=True).start()
        return {"ok": True}

    def _update_worker(self, gen):
        try:
            self._log("Updating yt-dlp...")
            result = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                                     capture_output=True, text=True)
            if result.stdout:
                self._log(result.stdout.strip())
            if result.stderr:
                self._log(result.stderr.strip())
            if result.returncode != 0:
                raise RuntimeError(f"pip exited with code {result.returncode}.")
            if gen == self._gen:
                self._set_done("yt-dlp has been updated.")
        except Exception as exc:
            if gen == self._gen:
                self._set_error("Update failed", exc)
        finally:
            self._finish(gen)


def json_body():
    return request.get_json(silent=True) or {}


app = Flask(__name__)
CORS(app, origins=ALLOWED_ORIGINS)
api = Api()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_html(filename, fallback):
    path = os.path.join(_SCRIPT_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return fallback


_INDEX_HTML = _read_html("index.html", "<h1>index.html was not found next to server.py</h1>")
_CONSOLE_HTML = _read_html("console.html", "<h1>console.html was not found next to server.py</h1>")


@app.get("/")
def index():
    return _INDEX_HTML


@app.get("/console")
def console_page():
    return _CONSOLE_HTML


@app.get("/api/console_log")
def console_log():
    return jsonify({"text": get_console_text()})


@app.get("/api/ping")
def ping():
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
def save_settings_route():
    return jsonify(api.save_settings(json_body()))


@app.post("/api/select_path")
def select_path_route():
    kind = json_body().get("kind", "file")
    path = api.select_path(kind)
    return jsonify({"path": path})


@app.post("/api/open_folder")
def open_folder_route():
    return jsonify(api.open_folder(json_body().get("path", "")))


@app.post("/api/open_search")
def open_search_route():
    return jsonify(api.open_search(json_body().get("query", "")))


@app.post("/api/fetch_info")
def fetch_info_route():
    body = json_body()
    return jsonify(api.fetch_info(body.get("url", ""), body.get("config", {})))


@app.post("/api/download")
def download_route():
    body = json_body()
    return jsonify(api.download(body.get("url", ""), body.get("config", {})))


@app.get("/api/poll")
def poll_route():
    return jsonify(api.poll())


@app.post("/api/convert_file")
def convert_file_route():
    body = json_body()
    return jsonify(api.convert_file(body.get("source", ""), body.get("target", "mp3")))


@app.post("/api/update_ytdlp")
def update_ytdlp_route():
    return jsonify(api.update_ytdlp())


def main():
    os.makedirs(UDL_DIR, exist_ok=True)
    print(f"{APP_NAME} (Termux) started. Server at http://{HOST}:{PORT}")
    server = make_server(HOST, PORT, app)
    server.serve_forever()


if __name__ == "__main__":
    main()
