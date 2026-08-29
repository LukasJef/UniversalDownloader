#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ytdlp_tray_app.py - tray app that keeps the local yt-dlp service running
in the background.

TERMINOLOGY NOTE: this is NOT a registered Windows service (via SCM /
services.msc) - those can't have a tray icon or any window (they run
isolated from the user's desktop). This is a regular application that
launches at login (via a registry Run key / LaunchAgent / XDG autostart
entry - see "Start with OS" in the tray menu) and behaves the same way
from the outside: it just keeps running in the background.

Left click on the tray icon  -> show/hide the log window ("console")
Right click on the tray icon -> "Open log" / "Start with OS" / "Quit"
Closing the log window (X)   -> just hides it, does NOT stop the background service
Quit in the menu             -> this is what actually stops the server and the app

Dependencies:
    pip install yt-dlp flask flask-cors pystray pillow

Run:
    python ytdlp_tray_app.py
"""

import os
import platform
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext

from PIL import Image, ImageDraw
import pystray
from werkzeug.serving import make_server

from ytdlp_local_server import app as flask_app, HOST, PORT, VERSION

APP_TITLE = "yt-dlp local service"
RUN_KEY_NAME = "YtDlpGuiTray"
BUNDLE_ID = "win.moviora.ytdlp-tray"  # used as the macOS LaunchAgent label

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"
IS_MACOS = SYSTEM == "Darwin"
IS_LINUX = SYSTEM == "Linux"


# --------------------------------------------------------------------------- #
#  Start at login - implementation for Windows / macOS / Linux               #
# --------------------------------------------------------------------------- #

def _startup_command_list():
    """Returns the command as a list of arguments (quoting is handled per-OS below)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def _startup_command_quoted():
    return " ".join(f'"{part}"' for part in _startup_command_list())


def _macos_launch_agent_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{BUNDLE_ID}.plist")


def _linux_autostart_path():
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(xdg_config, "autostart", "ytdlp-tray.desktop")


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
            # launchctl picks the agent up immediately, no logout/login needed
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
#  Flask server on a thread, with a real shutdown() (not bare app.run)       #
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
#  "Console" = a Tkinter window we redirect stdout/stderr into               #
# --------------------------------------------------------------------------- #

class TextRedirector:
    """Redirects print()/stdout into a Tkinter Text widget.

    IMPORTANT: write() can be called from any thread (the Flask server
    logs requests from its own thread) - but Tkinter/Tcl is NOT
    thread-safe, so we must never touch the widget directly here.
    Instead we just queue the text, and the main (Tkinter) thread drains
    the queue itself on a periodic root.after() tick.
    """

    def __init__(self, widget, root):
        self.widget = widget
        self.root = root
        self.queue = queue.Queue()
        self.root.after(100, self._drain)

    def write(self, text):
        if text:
            self.queue.put(text)

    def _drain(self):
        try:
            while True:
                text = self.queue.get_nowait()
                self._append(text)
        except queue.Empty:
            pass
        finally:
            try:
                self.root.after(100, self._drain)
            except tk.TclError:
                pass  # root was already destroyed (app is shutting down)

    def _append(self, text):
        try:
            self.widget.configure(state="normal")
            self.widget.insert("end", text)
            self.widget.see("end")
            self.widget.configure(state="disabled")
        except tk.TclError:
            pass  # window was closed in the meantime

    def flush(self):
        pass


class ConsoleWindow:
    def __init__(self, root):
        self.root = root
        self.top = tk.Toplevel(root)
        self.top.title(APP_TITLE)
        self.top.geometry("760x420")
        self.top.protocol("WM_DELETE_WINDOW", self.hide)  # the X button only hides it, never closes the app

        self.text = scrolledtext.ScrolledText(self.top, state="disabled", bg="black", fg="#33ff33",
                                              font=("Consolas", 10))
        self.text.pack(fill="both", expand=True)

        self.redirector = TextRedirector(self.text, root)
        sys.stdout = self.redirector
        sys.stderr = self.redirector

        self.top.withdraw()  # don't pop up on startup, just sit in the tray

    def toggle(self):
        if self.top.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def show(self):
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def hide(self):
        self.top.withdraw()


# --------------------------------------------------------------------------- #
#  Tray icon                                                                  #
# --------------------------------------------------------------------------- #

def make_icon_image():
    """A simple generated icon (a lightning bolt), so we don't need an external .ico file."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=(79, 70, 229, 255))
    draw.polygon([(36, 10), (20, 36), (30, 36), (26, 54), (46, 28), (35, 28)], fill=(255, 255, 255, 255))
    return img


def run_tray(root, console, server_thread):
    def on_toggle_console(icon, item):
        root.after(0, console.toggle)

    def on_toggle_autostart(icon, item):
        set_autostart(not is_autostart_enabled())

    def on_exit(icon, item):
        print("Shutting down...")
        server_thread.shutdown()
        icon.stop()
        root.after(0, root.destroy)

    menu = pystray.Menu(
        pystray.MenuItem("Open log", on_toggle_console, default=True),
        pystray.MenuItem("Start with OS", on_toggle_autostart,
                          checked=lambda item: is_autostart_enabled()),
        pystray.MenuItem("Quit", on_exit),
    )
    icon = pystray.Icon("ytdlp-tray", make_icon_image(), APP_TITLE, menu)
    icon.run()


def main():
    root = tk.Tk()
    root.withdraw()  # the root window is never shown, it only drives the mainloop

    console = ConsoleWindow(root)
    print(f"{APP_TITLE} started.")
    print(f"Server running at http://{HOST}:{PORT}  (version {VERSION})")

    server_thread = ServerThread(flask_app, HOST, PORT)
    server_thread.start()

    tray_thread = threading.Thread(target=run_tray, args=(root, console, server_thread), daemon=True)
    tray_thread.start()

    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
