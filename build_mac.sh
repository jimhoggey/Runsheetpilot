#!/bin/bash
# Build the Mac .app bundle and a distributable .dmg.
# Output: dist/Runsheet Pilot.app  and  dist/Runsheet-Pilot-<ver>.dmg
#
# Run:  ./build_mac.sh
# Clean: ./build_mac.sh clean

set -e
cd "$(dirname "$0")"

APP_NAME="Runsheet Pilot"
APP_BUNDLE_NAME="${APP_NAME}.app"
ENTRY="propresenter_app.py"

# Pull version from VERSION = "x.y.z". Lives in propresenterrunsheet/
# config.py since the refactor — propresenter_app.py just re-exports it.
VERSION_FILE="propresenterrunsheet/config.py"
VERSION=$(grep -E '^VERSION\s*=' "$VERSION_FILE" 2>/dev/null | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
[ -z "$VERSION" ] && VERSION="0.0.0"

DMG_NAME="Runsheet-Pilot-${VERSION}.dmg"

if [ "$1" = "clean" ]; then
    echo "Cleaning build/ dist/ *.spec ..."
    rm -rf build dist __pycache__
    find . -maxdepth 1 -name "*.spec" -delete
    echo "Done."
    exit 0
fi

echo "================================================================"
echo "  Building ${APP_NAME} v${VERSION} for macOS"
echo "================================================================"

# Sanity: Python and tools
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install via Homebrew:  brew install python"
    exit 1
fi
PY="python3"
echo "Python: $($PY --version)"

# Use a venv so we get a clean, predictable build (and pyinstaller picks the right Python)
VENV_DIR="build_venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating build venv at $VENV_DIR ..."
    $PY -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "Installing build dependencies ..."
pip install --upgrade pip --quiet
pip install -r requirements-dev.txt --quiet

echo "Cleaning previous build artifacts ..."
rm -rf build dist
find . -maxdepth 1 -name "*.spec" -delete

echo "Running PyInstaller ..."
pyinstaller \
    --name "${APP_NAME}" \
    --windowed \
    --onedir \
    --noconfirm \
    --clean \
    --osx-bundle-identifier "org.church.runsheetPilot" \
    --collect-submodules pdfplumber \
    --collect-submodules waitress \
    --collect-submodules cryptography \
    --hidden-import waitress \
    --hidden-import certifi \
    --collect-data certifi \
    --hidden-import flask \
    --hidden-import pdfplumber \
    --hidden-import tkinter \
    --collect-all webview \
    --collect-all ocrmac \
    --collect-all pypdfium2 \
    --hidden-import Vision \
    --hidden-import CoreML \
    --icon assets/icon.icns \
    --add-data "templates:templates" \
    --add-data "assets:assets" \
    --add-data "static:static" \
    "$ENTRY"

if [ ! -d "dist/${APP_BUNDLE_NAME}" ]; then
    echo "ERROR: Build did not produce dist/${APP_BUNDLE_NAME}"
    exit 1
fi

# ── Local Network permission (macOS 15+) ─────────────────────────────────────
# Service Mate reaches GeekMagic clocks on the LAN by IP, and ProPresenter can
# be on another machine the same way. Since macOS 15 that needs Local Network
# access, and WITHOUT this key the OS denies it — the app then sees
# `[Errno 65] No route to host` for a device that answers curl from Terminal
# in the same second. It looks exactly like a dead clock or a wrong IP, which
# is how it was misdiagnosed twice before the cause was found.
#
# The string is what macOS shows in its permission prompt, so it is written
# for the operator rather than for us.
PLIST="dist/${APP_BUNDLE_NAME}/Contents/Info.plist"
plutil -replace NSLocalNetworkUsageDescription \
    -string "Runsheet Pilot needs Local Network access to reach ProPresenter and your Service Mate clocks on this network." \
    "$PLIST"
plutil -lint "$PLIST" >/dev/null || { echo "ERROR: Info.plist is malformed"; exit 1; }
echo "Info.plist: Local Network usage description added"

echo "App bundle built: dist/${APP_BUNDLE_NAME}"

# ── Make the .dmg ────────────────────────────────────────────────────────────
echo ""
echo "Creating .dmg ..."
DMG_STAGE="dist/dmg_stage"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE"
cp -R "dist/${APP_BUNDLE_NAME}" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"

rm -f "dist/${DMG_NAME}"
hdiutil create \
    -volname "${APP_NAME}" \
    -srcfolder "$DMG_STAGE" \
    -ov \
    -format UDZO \
    "dist/${DMG_NAME}" >/dev/null

rm -rf "$DMG_STAGE"

echo ""
echo "================================================================"
echo "  ✅ Done"
echo "  App: dist/${APP_BUNDLE_NAME}"
echo "  DMG: dist/${DMG_NAME}"
echo "================================================================"
echo ""
echo "Note: The .app is unsigned. On first launch, users must"
echo "      right-click → Open (Gatekeeper warning is normal)."
