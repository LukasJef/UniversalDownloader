#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ytdlp_local_server.py - lokální HTTP "helper" služba pro webovou verzi appky.

Poslouchá JEN na 127.0.0.1 (nikdy ne na 0.0.0.0), takže není dostupná z jiných
počítačů v síti - jen z prohlížeče na téhle stanici. Statická stránka (např.
na pages.dev) na ni volá přes fetch() a získává tak plnou funkčnost yt-dlp,
aniž by uživatel musel cokoliv instalovat kromě týhle jedné služby.

Dependencies:
    pip install yt-dlp flask flask-cors

Spuštění:
    python ytdlp_local_server.py

Bezpečnost: ALLOWED_ORIGINS níže omez jen na doménu(y), odkud appku fakticky
hostuješ. Nikdy nedávej "*" - jakákoliv otevřená stránka v prohlížeči by pak
mohla tuhle lokální službu volat a spouštět stahování / číst složky na disku.
"""

import os
import sys

from flask import Flask, jsonify, request
from flask_cors import CORS

from ytdlp_core import Api, yt_dlp

# --------------------------------------------------------------------------- #
#  Konfigurace                                                                #
# --------------------------------------------------------------------------- #

PORT = int(os.environ.get("YTDLP_LOCAL_PORT", "47831"))
HOST = "127.0.0.1"  # NIKDY neměnit na 0.0.0.0 - jen lokální přístup!

# Sem patří doména(y), odkud appku hostuješ (pages.dev projekt) + localhost
# pro vývoj. Wildcard "*" záměrně nepoužívej (viz bezpečnostní poznámka výše).
ALLOWED_ORIGINS = [
    "https://udl.moviora.win",
    "http://localhost:5500",   # např. VS Code Live Server při vývoji stránky
    "http://127.0.0.1:5500",
]

VERSION = "1.0.0"

app = Flask(__name__)
CORS(app, origins=ALLOWED_ORIGINS)

api = Api()


# --------------------------------------------------------------------------- #
#  Pomocné funkce pro parsování požadavků                                     #
# --------------------------------------------------------------------------- #

def json_body():
    return request.get_json(silent=True) or {}


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


def main():
    if yt_dlp is None:
        print("Upozornění: chybí yt-dlp. Nainstaluj: pip install yt-dlp")
    print(f"Lokální yt-dlp služba běží na http://{HOST}:{PORT}")
    print(f"Povolené domény (CORS): {ALLOWED_ORIGINS}")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    sys.exit(main())
