#!/bin/bash
# Runsheet Pilot — Mac Launcher
# Run this from Terminal, or right-click → Open With → Terminal

echo "================================================"
echo " Runsheet Pilot"
echo "================================================"
echo ""

if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 not found."
    echo "Install via Homebrew:  brew install python"
    echo "Or from:               https://python.org"
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Python: $(python3 --version)"
echo ""
echo "Installing / updating dependencies..."
python3 -m pip install -r requirements.txt --quiet --upgrade --break-system-packages 2>/dev/null \
  || python3 -m pip install -r requirements.txt --quiet --upgrade

echo ""
echo "Starting server — your browser will open automatically."
echo "Press Ctrl+C to quit."
echo ""

cd "$(dirname "$0")"
python3 propresenter_app.py
