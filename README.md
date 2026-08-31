# UniversalDownloader | UDL
A single cross-platform app that combines a local [yt-dlp](https://github.com/yt-dlp/yt-dlp) service, a system tray icon, and an on-demand desktop window — all in one process. The same local service also powers the companion web UI at **[udl.moviora.win](https://udl.moviora.win)**, so the desktop app and the website share one codebase and one interface.

## Features

- **Video** — pick quality, audio/dub language, container (mp4/mkv/webm/avi), optional subtitle embedding
- **Audio** — pick track/language, optional conversion (mp3/m4a/wav/flac/ogg) via ffmpeg
- **Subtitles** — original or auto-generated, choice of format (best/srt/vtt)
- **Thumbnails** — pick resolution, optional image conversion (jpg/png/webp) via ffmpeg
- **Playlists** — full playlist download support across all modes above
- **Cookies** — none, `cookies.txt` file, or straight from your browser
- **Download queue** — queue up several URLs and let them run one after another
- **Rename** — custom output filename instead of the original title
- **Speed limit**, **FFmpeg file converter** tool, **update-check** for yt-dlp
- **5 languages** (Czech, English, Polish, French, Japanese) — auto-detected from your system/browser, remembers a manual override
- **Dark/light theme**

## How it's put together

`ytdlp_app.py` is a single file with three parts:

1. **Engine** — settings, yt-dlp calls, ffmpeg conversions (`Api` class)
2. **Local HTTP server** (Flask, `127.0.0.1` only) — exposes the engine over HTTP and serves `index.html`
3. **Desktop shell** — tray icon, on-demand window (pywebview), global hotkey, autostart

The desktop window is just another client of `http://127.0.0.1:47831/` — exactly like a browser tab. That's what lets [udl.moviora.win](https://udl.moviora.win) talk to the same local service (with CORS locked down to that domain only) and share one frontend (`index.html`) with the desktop app.

### Tray icon

| Action | Result |
|---|---|
| Left click | Open/focus the app window |
| `Win`+`Shift`+`D` (`Cmd`+`Shift`+`D` on macOS) | Same as left click, works from anywhere |
| Right click → **Open** | Open/focus the app window |
| Right click → **Open log** | Open the app window on the Logs tab |
| Right click → **Run with OS** | Toggle launching automatically at login |
| Right click → **Exit** | Stop the local service and quit |

Closing the app window normally (the X button) just closes that window — the service keeps running in the tray, and opening it again creates a fresh window.

## Installation

### Download a prebuilt binary

Grab the latest build for your OS from the [Releases](../../releases) page.

- **Windows** — unzip and run `ytdlp-app.exe`
- **macOS** — unzip, then run/move `ytdlp-app` (or `ytdlp-app.app`, if built as a bundle)
- **Linux** — unzip, `chmod +x ytdlp-app`, then run it

You'll also need **[ffmpeg](https://ffmpeg.org/download.html)** on your `PATH` for merging video+audio, format conversion, and embedding subtitles.

### Run from source

```bash
pip install -r requirements.txt
python ytdlp_app.py
```

Linux additionally needs system packages for the WebKitGTK renderer and tray icon:

```bash
sudo apt-get install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-ayatanaappindicator3-0.1
```

> Global hotkeys on Linux currently require an X11 session (Xorg) — Wayland restricts global key listening for security reasons and isn't supported yet.

## Building it yourself

```bash
pyinstaller --onefile --windowed --name ytdlp-app ytdlp_app.py
```

GitHub Actions (`.github/workflows/build.yml`) does this automatically for Windows, macOS, and Linux whenever a tag matching `v*` is pushed, and attaches all three builds to the resulting GitHub Release. You can also trigger it manually from the **Actions** tab (`workflow_dispatch`) to test a build without cutting a release.

## Web version

[udl.moviora.win](https://udl.moviora.win) is a static page that talks to the very same local service running on your machine at `127.0.0.1:47831`. If the service isn't detected, the page offers the installer for your OS instead. No separate web backend — it's the same `ytdlp_app.py` you already run locally.

## Project layout

```
ytdlp_app.py               single unified app (engine + local server + desktop shell)
index.html                 shared frontend for the desktop window and the website
requirements.txt
.github/workflows/build.yml
```

## Disclaimer

This tool uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to download publicly available media. You're responsible for complying with the terms of service of whatever site you download from, and with applicable copyright law in your jurisdiction.
