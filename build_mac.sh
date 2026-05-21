#!/bin/bash
# Build the Mac .app bundle and a distributable .dmg.
# Output: dist/ProPresenter Runsheet Builder.app  and  dist/ProPresenter-Runsheet-Builder-<ver>.dmg
#
# Run:  ./build_mac.sh
# Clean: ./build_mac.sh clean

set -e
cd "$(dirname "$0")"

APP_NAME="ProPresenter Runsheet Builder"
APP_BUNDLE_NAME="${APP_NAME}.app"
ENTRY="propresenter_app.py"

# Pull version from VERSION = "x.y.z". Lives in propresenterrunsheet/
# config.py since the refactor — propresenter_app.py just re-exports it.
VERSION_FILE="propresenterrunsheet/config.py"
VERSION=$(grep -E '^VERSION\s*=' "$VERSION_FILE" 2>/dev/null | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
[ -z "$VERSION" ] && VERSION="0.0.0"

DMG_NAME="ProPresenter-Runsheet-Builder-${VERSION}.dmg"

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
    --onefile \
    --noconfirm \
    --clean \
    --osx-bundle-identifier "org.church.proPresenterRunsheetBuilder" \
    --collect-submodules pdfplumber \
    --collect-submodules waitress \
    --hidden-import waitress \
    --hidden-import flask \
    --hidden-import pdfplumber \
    --hidden-import tkinter \
    --add-data "templates:templates" \
    --add-data "static:static" \
    "$ENTRY"

if [ ! -d "dist/${APP_BUNDLE_NAME}" ]; then
    echo "ERROR: Build did not produce dist/${APP_BUNDLE_NAME}"
    exit 1
fi

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
