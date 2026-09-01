#!/data/data/com.termux/files/usr/bin/bash
# setup.sh - jednorazova priprava Termuxu pro UniversalDownloader.
#
# Spustit uvnitr Termuxu (ne v Android appce):
#   curl -o setup.sh https://raw.githubusercontent.com/LukasJef/UniversalDownloader/main/termux/setup.sh
#   bash setup.sh
#
# Co to dela:
#   1) nainstaluje python + ffmpeg (realne, plnohodnotne - zadny ffmpeg-kit)
#   2) nainstaluje yt-dlp/flask pres pip
#   3) stahne server.py + index.html + console.html do ~/udl/
#   4) zpristupni Android slozku Download uvnitr Termuxu (~/storage/downloads)
#   5) povoli, aby appka UniversalDownloader mohla Termuxu posilat prikazy

set -e

REPO_RAW_BASE="https://raw.githubusercontent.com/LukasJef/UniversalDownloader/main"

echo "== UniversalDownloader - priprava Termuxu =="
echo

echo "[1/5] Instaluji python a ffmpeg..."
pkg update -y
pkg install -y python ffmpeg

echo
echo "[2/5] Instaluji yt-dlp, flask, flask-cors..."
pip install --upgrade pip
pip install yt-dlp flask flask-cors

echo
echo "[3/5] Stahuji server.py, index.html, console.html do ~/udl/..."
mkdir -p ~/udl
curl -fsSL "$REPO_RAW_BASE/termux/server.py" -o ~/udl/server.py
curl -fsSL "$REPO_RAW_BASE/index.html" -o ~/udl/index.html
curl -fsSL "$REPO_RAW_BASE/console.html" -o ~/udl/console.html
chmod +x ~/udl/server.py

echo
echo "[4/5] Zpristupnuji slozku Download (potreba potvrdit v appce)..."
termux-setup-storage
sleep 2  # dat systemu chvili na zpracovani povoleni

echo
echo "[5/5] Povoluji, aby appka UniversalDownloader mohla spoustet prikazy v Termuxu..."
mkdir -p ~/.termux
PROPS=~/.termux/termux.properties
if [ -f "$PROPS" ] && grep -q "^allow-external-apps" "$PROPS"; then
    sed -i 's/^allow-external-apps.*/allow-external-apps = true/' "$PROPS"
else
    echo "allow-external-apps = true" >> "$PROPS"
fi
termux-reload-settings

echo
echo "== Hotovo =="
echo "Muzes zkusit rucne spustit server prikazem:"
echo "    python ~/udl/server.py"
echo "a otevrit http://127.0.0.1:47831 v prohlizeci na telefonu."
echo
echo "Pro plnou funkcnost (sdileni z YouTube atd.) nainstaluj Android appku"
echo "UniversalDownloader - viz README v repozitari."
