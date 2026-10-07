#!/bin/sh
# Builds Firely's engine (llama.cpp + android/app/src/main/cpp) for the phone with the Android NDK,
# into android/app/src/main/jniLibs:
#   arm64-v8a/libbrain.so          works on every 64-bit ARM phone
#   arm64-v8a/libbrain_dotprod.so  about twice as fast, for phones from ~2018 on (chosen at runtime by Brain.kt)
#   x86_64/libbrain.so             emulators and Chromebooks
set -e
cd "$(dirname "$0")/../.."
NDK="${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_HOME:-$(ls -d "$ANDROID_HOME"/ndk/* | sort -V | tail -1)}}"
SRC=android/app/src/main/cpp
OUT=android/app/src/main/jniLibs
BUILD="${BRAIN_BUILD_DIR:-build/brain}"
echo "Using NDK $NDK"
mkdir -p "$BUILD"
GEN=Ninja; command -v ninja > /dev/null || GEN="Unix Makefiles"

build() {  # abi name extra-cmake-args...
  abi=$1; name=$2; shift 2
  cmake -S "$SRC" -B "$BUILD/$abi-$name" -G "$GEN" \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI="$abi" -DANDROID_PLATFORM=android-24 -DCMAKE_BUILD_TYPE=Release \
    -DBRAIN_NAME="$name" "$@" > "$BUILD/$abi-$name.cfg.log" 2>&1 || { tail -40 "$BUILD/$abi-$name.cfg.log"; exit 1; }
  cmake --build "$BUILD/$abi-$name" --target brain -j "$(nproc)"
  mkdir -p "$OUT/$abi"
  "$NDK"/toolchains/llvm/prebuilt/*/bin/llvm-strip --strip-unneeded -o "$OUT/$abi/lib$name.so" "$BUILD/$abi-$name/lib$name.so"
}

build arm64-v8a brain
build arm64-v8a brain_dotprod -DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16
build x86_64 brain
ls -la "$OUT"/*/libbrain*.so
