# UniversalDownloader (UDL)

A single cross-platform app that combines a local [yt-dlp](https://github.com/yt-dlp/yt-dlp) service, a system tray icon, and an on-demand desktop window — all in one process. The same local service also powers the companion web UI at **[udl.moviora.win](https://udl.moviora.win)**, so the desktop app and the website share one codebase and one interface.

## Features

- **Video** — pick quality, audio/dub language (or **embed all available audio tracks at once**, e.g. original + dubs), container (mp4/mkv/webm/avi), optional subtitle embedding
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
- **Hidden diagnostic console** (`/console`) — raw stdout/stderr, useful for bug reports; not shown anywhere in the normal UI

## How it's put together

`ytdlp_app.py` is a single file with three parts:

1. **Engine** — settings, yt-dlp calls, ffmpeg conversions (`Api` class)
2. **Local HTTP server** (Flask, `127.0.0.1` only) — exposes the engine over HTTP and serves `index.html` / `console.html`
3. **Desktop shell** — tray icon, on-demand window (pywebview), global hotkey, autostart

The desktop window is just another client of `http://127.0.0.1:47831/` — exactly like a browser tab. That's what lets [udl.moviora.win](https://udl.moviora.win) talk to the same local service (with CORS locked down to that domain only) and share one frontend (`index.html`) with the desktop app.

### Tray icon

| Action | Result |
|---|---|
| Left click | Open/focus the app window |
| `Win`+`Shift`+`D` (`Cmd`+`Shift`+`D` on macOS) | Same as left click, works from anywhere |
| Right click → **Open** | Open/focus the app window |
| Right click → **Open Console** | Open the app window on the hidden `/console` page (raw stdout/stderr — for debugging, not everyday use) |
| Right click → **Run with OS** | Toggle launching automatically at login |
| Right click → **Exit** | Stop the local service and quit |

Closing the app window normally (the X button) just closes that window — the service keeps running in the tray, and opening it again creates a fresh window.

## Installation

### Download a prebuilt binary

Grab the latest build for your OS from the [Releases](../../releases) page.

- **Windows** — unzip and run `UniversalDownloader.exe`
- **macOS** — unzip, then run/move `UniversalDownloader` (or `UniversalDownloader.app`, if built as a bundle)
- **Linux** — unzip, `chmod +x UniversalDownloader`, then run it

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
pyinstaller --onefile --windowed --name UniversalDownloader --add-data "index.html:." --add-data "console.html:." ytdlp_app.py
```

(On Windows use `;` instead of `:` as the separator in `--add-data`.)

GitHub Actions (`.github/workflows/build.yml`) does this automatically for Windows, macOS, and Linux whenever a tag matching `v*` is pushed, and attaches all three builds to the resulting GitHub Release. You can also trigger it manually from the **Actions** tab (`workflow_dispatch`) to test a build without cutting a release.

## Web version

[udl.moviora.win](https://udl.moviora.win) is a static page that talks to the very same local service running on your machine at `127.0.0.1:47831`. If the service isn't detected, the page offers the installer for your OS instead. No separate web backend — it's the same `ytdlp_app.py` you already run locally.

## Android

Instead of bundling a whole Python/ffmpeg runtime inside the app (which was tried
with Chaquopy + [ffmpeg-kit-extended](https://github.com/akashskypatel/ffmpeg-kit-extended)
and turned out fragile - broken builds, missing native libraries, huge APKs),
the Android app takes a simpler approach: it runs the **exact same engine**
that powers the desktop app and the website, inside [Termux](https://termux.dev/)
- a real Linux environment for Android with a genuine `yt-dlp` and a genuine
`ffmpeg`, no compromises.

### How it works

1. **Termux** runs `termux/server.py` - basically `ytdlp_app.py`'s engine and
   Flask server, without the desktop-only parts (tray icon, pywebview window,
   autostart). It listens on `127.0.0.1:47831`, exactly like the desktop app.
   The setup script below makes it start automatically every time you open
   Termux, so it's normally just running quietly in the background.
2. The **UniversalDownloader Android app** (`android/` folder) doesn't embed
   Python at all. It just checks whether that server is reachable and shows
   the same `index.html` in a `WebView` - identical UI to desktop and web.
3. Sharing a link from another app ("Share" to UniversalDownloader) forwards
   the URL straight into that `WebView`, same as the desktop share flow.

No data leaves your phone - `127.0.0.1` never goes over the network.

### Setup (one-time)

1. **Install Termux** from [F-Droid](https://f-droid.org/packages/com.termux/)
   or the [GitHub releases](https://github.com/termux/termux-app/releases) -
   **not** the Play Store version, which is outdated and no longer maintained.
2. Open Termux and run the setup script:
   ```bash
   curl -o setup.sh https://raw.githubusercontent.com/LukasJef/UniversalDownloader/main/termux/setup.sh
   bash setup.sh
   ```
   This installs `python`/`ffmpeg`, installs `yt-dlp`/`flask`/`flask-cors`,
   downloads `server.py` + `index.html` + `console.html` into `~/udl/`, runs
   `termux-setup-storage` (so downloads land in your normal Android Downloads
   folder, not hidden inside Termux), and adds a small snippet to `~/.bashrc`
   so the server starts by itself every time you open Termux from then on.
3. **Install the UniversalDownloader Android app** - either grab the APK from
   [Releases](../../releases) (once available for your version) or build it
   yourself from the `android/` folder in Android Studio.
4. That's it. Just make sure Termux has been opened at least once (so the
   server is running in the background), then open the app or share a link
   to it - it'll find the server at `127.0.0.1:47831` automatically.

> **Why not fully automatic?** The app *can* try to ask Termux to start the
> server itself, using Termux's official `RUN_COMMAND` intent. In practice
> Android usually won't let it - custom permissions declared by another app
> (rather than by Android itself) often don't show up as a grantable toggle
> in Settings, and the only reliable way to grant it is via `adb`:
> ```bash
> adb shell pm grant win.moviora.udl com.termux.permission.RUN_COMMAND
> ```
> This is a known limitation of Android's permission system (the same one
> official Termux plugins like Termux:Tasker run into), not something this
> app can fix on its own. It's entirely optional - the auto-start snippet
> from step 2 already covers normal use without it.

### Important: Android 12+ kills background processes

This is the single most likely reason for the app to say the server isn't
running even though you just started it. Since Android 12, the system has a
**phantom process killer** that silently kills processes spawned by apps
(exactly what our Python server is) once a system-wide limit is hit or when
the parent app goes to the background. It's a
[well-known problem](https://github.com/termux/termux-app/issues/2366) that
breaks Termux generally, not something this app can work around from its
own code.

Symptoms: the server works while Termux is in the foreground, then stops
responding the moment you switch to another app - or works only if you open
the apps in a particular order.

#### You'll probably need these

In practice the app is unlikely to work reliably without both of these:

**1. Turn off the child-process limit.** Settings -> About phone -> tap
**Build number** 7 times to unlock Developer options, then Settings ->
System -> **Developer options** and turn off the setting that limits child
processes (its exact name varies by manufacturer - on some phones it's a
"Feature flags" entry called `settings_enable_monitor_phantom_procs`, on
others a plain toggle like "Disable child process restrictions").

If your phone has no such toggle at all, the same thing can be done with
one `adb` command from a computer:

```bash
adb shell "/system/bin/device_config put activity_manager max_phantom_processes 2147483647"
```

**2. Stop Android from putting Termux to sleep.** Settings -> Apps ->
**Termux** -> Battery -> **Unrestricted** (wording varies: "No
restrictions", "Don't optimize", etc.).

#### Recommended as well

**Raise the background process limit.** In Developer options, set
**Background process limit** to the highest value your phone offers.
On its own this usually isn't enough to fix the problem, but it helps once
the two settings above are in place.

Also: leave Termux running in the background rather than swiping it away
from the recent-apps list.

### Known limitations

- No native folder picker yet (downloads always go to your phone's normal
  Downloads folder).
- `Open folder` / `Find manually` (opening a browser search) need the
  separate `Termux:API` add-on (`pkg install termux-api` + the Termux:API
  app from F-Droid) - optional, everything else works without it.
- Only one client at a time gets the live log messages, so don't keep the
  app and the website open side by side - they'll each see only part of it.

## Project layout

```
ytdlp_app.py               single unified desktop app (engine + local server + desktop shell)
index.html                  shared frontend for desktop, web, and Android
console.html                 hidden raw stdout/stderr viewer (/console)
requirements.txt
.github/workflows/build.yml
termux/server.py            same engine, adapted to run standalone inside Termux (Android)
termux/setup.sh              one-time Termux setup script (see Android section above)
android/                    Android app - orchestrates the Termux server, shows index.html in a WebView
```

## Disclaimer

This tool uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to download publicly available media. You're responsible for complying with the terms of service of whatever site you download from, and with applicable copyright law in your jurisdiction.
