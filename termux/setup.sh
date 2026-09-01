#!/data/data/com.termux/files/usr/bin/bash
# setup.sh - one-time setup of Termux for UniversalDownloader.
#
# Run this inside Termux (not in the Android app):
#   curl -o setup.sh https://raw.githubusercontent.com/LukasJef/UniversalDownloader/main/termux/setup.sh
#   bash setup.sh
#
# What it does:
#   1) installs python + ffmpeg (real, full-featured - no ffmpeg-kit)
#   2) installs yt-dlp/flask via pip
#   3) downloads server.py + index.html + console.html into ~/udl/
#   4) exposes the Android Download folder inside Termux (~/storage/downloads)
#   5) allows the UniversalDownloader app to send commands to Termux

set -e

REPO_RAW_BASE="https://raw.githubusercontent.com/LukasJef/UniversalDownloader/main"

echo "== UniversalDownloader - setting up Termux =="
echo

echo "[1/5] Installing python, pip and ffmpeg..."
pkg update -y
pkg install -y python python-pip ffmpeg

echo
echo "[2/5] Installing yt-dlp, flask, flask-cors..."
# NOTE: do NOT run "pip install --upgrade pip" in Termux - pip here is
# managed by the python-pip package above, and self-upgrading it via pip
# is blocked by Termux on purpose ("installing pip is forbidden, this will
# break the python-pip package") to keep it in sync with the Python build.
pip install yt-dlp flask flask-cors

echo
echo "[3/5] Downloading server.py, index.html, console.html into ~/udl/..."
mkdir -p ~/udl
# Zastavime pripadny bezici server jeste PRED stazenim - jinak by dal bezel
# na stare verzi souboru (server.py i index.html se ctou jen jednou pri startu)
# a uzivatel by po aktualizaci nevidel zadnou zmenu.
if [ -f ~/udl/server.pid ]; then
    kill "$(cat ~/udl/server.pid)" 2>/dev/null && echo "    (stopping the running server first)"
    rm -f ~/udl/server.pid
    sleep 1
fi
curl -fsSL "$REPO_RAW_BASE/termux/server.py" -o ~/udl/server.py
curl -fsSL "$REPO_RAW_BASE/index.html" -o ~/udl/index.html
curl -fsSL "$REPO_RAW_BASE/console.html" -o ~/udl/console.html
chmod +x ~/udl/server.py

echo
echo "[4/5] Exposing the Download folder (you may need to confirm a permission prompt)..."
termux-setup-storage
sleep 2  # give the system a moment to process the permission

echo
echo "[5/5] Setting up the server to start automatically whenever you open Termux..."
# Android often won't let a third-party app (like the UniversalDownloader
# app) send the special RUN_COMMAND intent to Termux without a manual ADB
# permission grant, which isn't realistic to ask regular users to do. So
# instead of relying on that, we make the server start itself - simply
# opening Termux once is enough, no extra permission needed at all.
BASHRC=~/.bashrc
START_SNIPPET='# --- UniversalDownloader: auto-start local server ---'
if ! grep -qF "$START_SNIPPET" "$BASHRC" 2>/dev/null; then
    cat >> "$BASHRC" << 'EOF'

# --- UniversalDownloader: auto-start local server ---
UDL_PID_FILE=~/udl/server.pid
if [ -f ~/udl/server.py ]; then
    if [ ! -f "$UDL_PID_FILE" ] || ! kill -0 "$(cat "$UDL_PID_FILE" 2>/dev/null)" 2>/dev/null; then
        # Keeps the CPU awake so Android is less eager to suspend the server.
        # (Does NOT bypass Android 12+'s phantom process killer - see README.)
        termux-wake-lock 2>/dev/null
        nohup python ~/udl/server.py > ~/udl/server.log 2>&1 &
        echo $! > "$UDL_PID_FILE"
    fi
fi
# --- end UniversalDownloader ---
EOF
fi

# Start it right now too, for this session, so it's ready immediately.
UDL_PID_FILE=~/udl/server.pid
if [ ! -f "$UDL_PID_FILE" ] || ! kill -0 "$(cat "$UDL_PID_FILE" 2>/dev/null)" 2>/dev/null; then
    termux-wake-lock 2>/dev/null
    nohup python ~/udl/server.py > ~/udl/server.log 2>&1 &
    echo $! > "$UDL_PID_FILE"
fi

echo
echo "== Done =="
echo "The server now starts automatically every time you open Termux - just"
echo "open the Termux app (it can stay in the background) and the"
echo "UniversalDownloader Android app will find it at http://127.0.0.1:47831."
echo
echo "IMPORTANT (Android 12 and newer): Android has a 'phantom process killer'"
echo "that silently kills background processes started by apps like Termux."
echo "If the server keeps dying when you switch away from Termux, you need to"
echo "turn that off - see the Android section of the README for how."
