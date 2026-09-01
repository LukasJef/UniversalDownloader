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
curl -fsSL "$REPO_RAW_BASE/termux/server.py" -o ~/udl/server.py
curl -fsSL "$REPO_RAW_BASE/index.html" -o ~/udl/index.html
curl -fsSL "$REPO_RAW_BASE/console.html" -o ~/udl/console.html
chmod +x ~/udl/server.py

echo
echo "[4/5] Exposing the Download folder (you may need to confirm a permission prompt)..."
termux-setup-storage
sleep 2  # give the system a moment to process the permission

echo
echo "[5/5] Allowing the UniversalDownloader app to run commands in Termux..."
mkdir -p ~/.termux
PROPS=~/.termux/termux.properties
if [ -f "$PROPS" ] && grep -q "^allow-external-apps" "$PROPS"; then
    sed -i 's/^allow-external-apps.*/allow-external-apps = true/' "$PROPS"
else
    echo "allow-external-apps = true" >> "$PROPS"
fi
termux-reload-settings

echo
echo "== Done =="
echo "You can try starting the server manually with:"
echo "    python ~/udl/server.py"
echo "and opening http://127.0.0.1:47831 in a browser on your phone."
echo
echo "For full functionality (sharing links from YouTube etc.), install the"
echo "UniversalDownloader Android app - see the README in the repository."
