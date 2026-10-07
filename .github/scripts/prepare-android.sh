#!/bin/sh
# Copies the editor (Python engine, phone screen, caption fonts) into the Android project,
# and fetches what runs on the phone: the speech recognition library and model used for captions,
# and Firely, the thinking AI (llama.cpp source and the Qwen3-VL 2B model).
set -e
cd "$(dirname "$0")/../.."
PY=android/app/src/main/python
ASSETS=android/app/src/main/assets/editor
SPEECH=android/app/src/main/assets/speech
BRAIN=android/app/src/main/assets/brain
LLAMA_DIR=android/app/src/main/cpp/llama.cpp
LLAMA_PY=0.3.36  # llama-cpp-python release whose bundled llama.cpp we build (same source used in tests)
BRAIN_REPOS="${BRAIN_REPOS:-Qwen/Qwen3-VL-2B-Instruct-GGUF ggml-org/Qwen3-VL-2B-Instruct-GGUF unsloth/Qwen3-VL-2B-Instruct-GGUF}"
LIBS=android/app/libs
CACHE="${EDITOR_DOWNLOADS:-$HOME/.cache/editor-downloads}"
SHERPA=1.13.8
MODEL=sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc-int8-2025-11-12
rm -rf "$PY" "$ASSETS" "$SPEECH" "$BRAIN" "$LLAMA_DIR"
mkdir -p "$PY/engine" "$ASSETS" "$SPEECH" "$BRAIN" "$LIBS" "$CACHE"
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
# Firely: llama.cpp source (from the llama-cpp-python package on PyPI, which bundles it)
if [ ! -d "$CACHE/llama-cpp-python-$LLAMA_PY/vendor/llama.cpp" ]; then
  python3 -m pip download --no-deps --no-binary :all: "llama-cpp-python==$LLAMA_PY" -d "$CACHE" -q
  tar xzf "$CACHE/llama_cpp_python-$LLAMA_PY.tar.gz" -C "$CACHE"
  mv "$CACHE/llama_cpp_python-$LLAMA_PY" "$CACHE/llama-cpp-python-$LLAMA_PY"
fi
cp -r "$CACHE/llama-cpp-python-$LLAMA_PY/vendor/llama.cpp" "$LLAMA_DIR"

# Firely: the model (text) and its eyes (vision projector), from the first repository that has them
if [ ! -s "$CACHE/brain/model.gguf" ] || [ ! -s "$CACHE/brain/vision.gguf" ]; then
  mkdir -p "$CACHE/brain"
  for repo in $BRAIN_REPOS; do
    files=$(curl -fsSL "https://huggingface.co/api/models/$repo" \
      | python3 -c "import json,sys; print('\n'.join(s['rfilename'] for s in json.load(sys.stdin).get('siblings', [])))") || continue
    main=$(echo "$files" | grep -iv mmproj | grep -i 'Q4_K_M\.gguf$' | head -1)
    proj=$(echo "$files" | grep -i mmproj | grep -i 'Q8_0\.gguf$' | head -1)
    [ -n "$proj" ] || proj=$(echo "$files" | grep -i mmproj | grep -i 'f16\.gguf$' | head -1)
    if [ -n "$main" ] && [ -n "$proj" ]; then
      echo "Firely model: $repo / $main + $proj"
      curl -fL --retry 4 -o "$CACHE/brain/model.gguf.part" "https://huggingface.co/$repo/resolve/main/$main"
      curl -fL --retry 4 -o "$CACHE/brain/vision.gguf.part" "https://huggingface.co/$repo/resolve/main/$proj"
      curl -fsL "https://huggingface.co/$repo/resolve/main/LICENSE" -o "$CACHE/brain/LICENSE.txt" \
        || curl -fsL "https://www.apache.org/licenses/LICENSE-2.0.txt" -o "$CACHE/brain/LICENSE.txt"
      mv "$CACHE/brain/model.gguf.part" "$CACHE/brain/model.gguf"
      mv "$CACHE/brain/vision.gguf.part" "$CACHE/brain/vision.gguf"
      echo "$repo $main $proj" > "$CACHE/brain/source.txt"
      break
    fi
  done
  [ -s "$CACHE/brain/model.gguf" ] || { echo "Couldn't download the Firely model"; exit 1; }
fi
cp "$CACHE/brain/model.gguf" "$CACHE/brain/vision.gguf" "$BRAIN/"
{ echo "Firely is built on Qwen3-VL by the Qwen team (Alibaba Cloud), licensed under Apache 2.0,"
  echo "and runs with llama.cpp (MIT licence). Model files: $(cat "$CACHE/brain/source.txt")"
  echo; cat "$CACHE/brain/LICENSE.txt"; } > "$BRAIN/NOTICE.txt"

echo "Prepared: $(find "$PY" -name '*.py' | wc -l) Python files, $(find "$ASSETS" -type f | wc -l) screen/font files, speech model $(du -sh "$SPEECH" | cut -f1), Firely $(du -sh "$BRAIN" | cut -f1)"
