#!/bin/sh
# Copies the editor (Python engine, phone screen, caption fonts) into the Android project,
# and fetches the on-phone speech recognition library and model used for captions.
set -e
cd "$(dirname "$0")/../.."
PY=android/app/src/main/python
ASSETS=android/app/src/main/assets/editor
SPEECH=android/app/src/main/assets/speech
LIBS=android/app/libs
CACHE="${EDITOR_DOWNLOADS:-$HOME/.cache/editor-downloads}"
SHERPA=1.13.8
MODEL=sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-int8-2025-11-12
rm -rf "$PY" "$ASSETS" "$SPEECH"
mkdir -p "$PY/engine" "$ASSETS" "$SPEECH" "$LIBS" "$CACHE"
cp app/server.py app/android_main.py "$PY/"
cp app/engine/*.py "$PY/engine/"
cp -r app/static "$ASSETS/static"
cp -r app/fonts "$ASSETS/fonts"

if [ ! -s "$CACHE/sherpa-onnx-$SHERPA.aar" ]; then
  curl -fL --retry 4 -o "$CACHE/sherpa-onnx-$SHERPA.aar.part" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/v$SHERPA/sherpa-onnx-$SHERPA.aar"
  mv "$CACHE/sherpa-onnx-$SHERPA.aar.part" "$CACHE/sherpa-onnx-$SHERPA.aar"
fi
cp "$CACHE/sherpa-onnx-$SHERPA.aar" "$LIBS/sherpa-onnx.aar"

if [ ! -s "$CACHE/$MODEL/model.int8.onnx" ]; then
  curl -fL --retry 4 "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$MODEL.tar.bz2" \
    | tar xj -C "$CACHE"
fi
cp "$CACHE/$MODEL/model.int8.onnx" "$CACHE/$MODEL/tokens.txt" "$SPEECH/"
cp "$CACHE/$MODEL/test_wavs/en.wav" "$SPEECH/selftest_en.wav"
echo "Prepared: $(find "$PY" -name '*.py' | wc -l) Python files, $(find "$ASSETS" -type f | wc -l) screen/font files, speech model $(du -sh "$SPEECH" | cut -f1)"
