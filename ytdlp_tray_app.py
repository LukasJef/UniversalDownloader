#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ytdlp_tray_app.py - tray aplikace, co drží lokální yt-dlp službu na pozadí.

POZNÁMKA K TERMINOLOGII: tohle NENÍ registrovaná Windows služba (přes SCM /
services.msc) - ty totiž nemůžou mít tray ikonu ani žádné okno (běží izolovaně
od plochy uživatele). Tohle je normální aplikace, která se spustí spolu
s přihlášením uživatele (přes registry Run klíč - viz "Spouštět s Windows"
v menu tray ikony) a chová se navenek stejně: běží pořád na pozadí.

Levý klik na tray ikonu   -> zobrazí/skryje log okno ("konzoli")
Pravý klik na tray ikonu  -> nabídne "Otevřít log" / "Spouštět s Windows" / "Ukončit"
Zavření log okna (křížek) -> jen ho schová, službu na pozadí NEVYPÍNÁ
Ukončit v menu            -> teprve tohle vypne server a celou appku

Dependencies:
    pip install yt-dlp flask flask-cors pystray pillow

Spuštění:
    python ytdlp_tray_app.py
"""

import os
import platform
import queue
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

IS_WINDOWS = platform.system() == "Windows"


# --------------------------------------------------------------------------- #
#  Spouštění s Windows (registry Run klíč) - no-op na jiných OS               #
# --------------------------------------------------------------------------- #

def _startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def is_autostart_enabled():
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, RUN_KEY_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enabled):
    if not IS_WINDOWS:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_KEY_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_KEY_NAME)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- #
#  Flask server ve vlákně, se skutečným shutdown() (ne holé app.run)          #
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
#  "Konzole" = Tkinter okno, do kterého přesměrujeme stdout/stderr            #
# --------------------------------------------------------------------------- #

class TextRedirector:
    """Přesměruje print()/stdout do Tkinter Text widgetu.

    DŮLEŽITÉ: write() může být volané z libovolného vlákna (Flask server
    loguje requesty ze svého vlastního vlákna) - Tkinter/Tcl ale NENÍ
    thread-safe, takže tady nesmíme sáhnout na widget přímo. Místo toho
    jen odložíme text do fronty a hlavní (Tkinter) vlákno si ji samo
    pravidelně vybírá přes root.after().
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
                pass  # root uz byl znicen (appka konci)

    def _append(self, text):
        try:
            self.widget.configure(state="normal")
            self.widget.insert("end", text)
            self.widget.see("end")
            self.widget.configure(state="disabled")
        except tk.TclError:
            pass  # okno mezitím zaniklo

    def flush(self):
        pass


class ConsoleWindow:
    def __init__(self, root):
        self.root = root
        self.top = tk.Toplevel(root)
        self.top.title(APP_TITLE)
        self.top.geometry("760x420")
        self.top.protocol("WM_DELETE_WINDOW", self.hide)  # křížek jen schová, nezavírá appku

        self.text = scrolledtext.ScrolledText(self.top, state="disabled", bg="black", fg="#33ff33",
                                              font=("Consolas", 10))
        self.text.pack(fill="both", expand=True)

        self.redirector = TextRedirector(self.text, root)
        sys.stdout = self.redirector
        sys.stderr = self.redirector

        self.top.withdraw()  # při startu appka nevyskakuje, jen sedí v tray

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
#  Tray ikona                                                                  #
# --------------------------------------------------------------------------- #

def make_icon_image():
    """Jednoduchá vygenerovaná ikonka (blesk), ať nepotřebujeme externí .ico soubor."""
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
        print("Ukončuji...")
        server_thread.shutdown()
        icon.stop()
        root.after(0, root.destroy)

    menu = pystray.Menu(
        pystray.MenuItem("Open Log", on_toggle_console, default=True),
        pystray.MenuItem("Run with windows", on_toggle_autostart,
                          checked=lambda item: is_autostart_enabled(), enabled=IS_WINDOWS),
        pystray.MenuItem("Stop", on_exit),
    )
    icon = pystray.Icon("ytdlp-tray", make_icon_image(), APP_TITLE, menu)
    icon.run()


def main():
    root = tk.Tk()
    root.withdraw()  # hlavní root okno nikdy neukazujeme, jen drží mainloop

    console = ConsoleWindow(root)
    print(f"{APP_TITLE} is running.")
    print(f"Server is running on http://{HOST}:{PORT}  (version {VERSION})")

    server_thread = ServerThread(flask_app, HOST, PORT)
    server_thread.start()

    tray_thread = threading.Thread(target=run_tray, args=(root, console, server_thread), daemon=True)
    tray_thread.start()

    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
