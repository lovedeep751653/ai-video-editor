#!/bin/sh
# Copies the editor (Python engine, phone screen, caption fonts) into the Android project.
set -e
cd "$(dirname "$0")/../.."
PY=android/app/src/main/python
ASSETS=android/app/src/main/assets/editor
rm -rf "$PY" "$ASSETS"
mkdir -p "$PY/engine" "$ASSETS"
cp app/server.py app/android_main.py "$PY/"
cp app/engine/*.py "$PY/engine/"
cp -r app/static "$ASSETS/static"
cp -r app/fonts "$ASSETS/fonts"
echo "Prepared: $(find "$PY" -name '*.py' | wc -l) Python files, $(find "$ASSETS" -type f | wc -l) screen/font files"
