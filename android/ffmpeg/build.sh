#!/usr/bin/env bash
# Builds the FFmpeg the Android app runs: ffmpeg + ffprobe for arm64-v8a with
# SVT-AV1, Opus and HTTPS (mbedTLS). Everything is linked in except Android's
# own C library, which is linked as usual (a fully static binary may not be
# able to resolve host names on a device). The binaries are copied into the app as
# app/src/main/jniLibs/arm64-v8a/libffmpeg.so and libffprobe.so: Android only
# lets an app run executables that are installed in its native-library folder,
# and only files named lib*.so are put there.
#
# Usage:  ANDROID_NDK_HOME=/path/to/ndk ./build.sh [workdir]
#         LINK=static ... builds fully static binaries into <workdir>/static
#         instead (not for the app: for testing on a PC with qemu-aarch64).
# Needs:  curl, tar, cmake, ninja, make, pkg-config. About 15-30 minutes.
set -euo pipefail

NDK="${ANDROID_NDK_HOME:?set ANDROID_NDK_HOME to the Android NDK (r28 or newer)}"
WORK="$(realpath -m "${1:-$PWD/build}")"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../app/src/main/jniLibs/arm64-v8a"

FFMPEG_VER=9.1
SVT_VER=v4.3.0
OPUS_VER=1.5.2
MBEDTLS_VER=3.6.4
API=24
TRIPLE=aarch64-linux-android
JOBS="$(nproc)"
LINK="${LINK:-dynamic}"

TC="$NDK/toolchains/llvm/prebuilt/linux-x86_64"
PREFIX="$WORK/prefix"
CC="$TC/bin/${TRIPLE}${API}-clang"
CXX="$TC/bin/${TRIPLE}${API}-clang++"
CMAKE_ANDROID=(-G Ninja -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake"
               -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-$API
               -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
               -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_LIBDIR=lib)

mkdir -p "$WORK/src" "$PREFIX"
cd "$WORK/src"

fetch() {  # url  dir
    [ -d "$2" ] && return
    echo "== downloading $1"
    curl -fsSL "$1" -o "$2.archive"
    mkdir "$2" && tar -xf "$2.archive" -C "$2" --strip-components=1 && rm "$2.archive"
}
fetch "https://downloads.xiph.org/releases/opus/opus-$OPUS_VER.tar.gz" opus
fetch "https://gitlab.com/AOMediaCodec/SVT-AV1/-/archive/$SVT_VER/SVT-AV1-$SVT_VER.tar.gz" svt
fetch "https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-$MBEDTLS_VER/mbedtls-$MBEDTLS_VER.tar.bz2" mbedtls
fetch "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VER.tar.xz" ffmpeg

if [ ! -f "$PREFIX/lib/libopus.a" ]; then
    echo "== opus"
    cmake -S opus -B opus/b "${CMAKE_ANDROID[@]}" -DOPUS_BUILD_PROGRAMS=OFF -DOPUS_BUILD_TESTING=OFF
    cmake --build opus/b -j "$JOBS" && cmake --install opus/b
fi
if [ ! -f "$PREFIX/lib/libSvtAv1Enc.a" ]; then
    echo "== SVT-AV1"
    cmake -S svt -B svt/b "${CMAKE_ANDROID[@]}" -DBUILD_APPS=OFF -DBUILD_DEC=OFF -DBUILD_TESTING=OFF
    cmake --build svt/b -j "$JOBS" && cmake --install svt/b
fi
if [ ! -f "$PREFIX/lib/libmbedtls.a" ]; then
    echo "== mbedTLS"
    cmake -S mbedtls -B mbedtls/b "${CMAKE_ANDROID[@]}" -DENABLE_PROGRAMS=OFF -DENABLE_TESTING=OFF
    cmake --build mbedtls/b -j "$JOBS" && cmake --install mbedtls/b
fi

echo "== FFmpeg $FFMPEG_VER"
cd ffmpeg
# FFmpeg verifies HTTPS certificates by default, and with mbedTLS it trusts
# only what -ca_file names: Android has no CA bundle file for it to find. Let
# SSL_CERT_FILE name one when -ca_file is not given (the app sets it to the
# Mozilla CA bundle that Python's certifi ships), so every ffmpeg/ffprobe call
# the worker makes verifies the server without passing extra options.
if ! grep -q SSL_CERT_FILE libavformat/tls_mbedtls.c; then
    python3 - <<'PY'
p = "libavformat/tls_mbedtls.c"
s = open(p).read()
old = "    // load trusted CA\n    if (shr->ca_file) {"
new = ("    // load trusted CA (Android build: SSL_CERT_FILE when -ca_file is not set)\n"
       "    if (!shr->ca_file && getenv(\"SSL_CERT_FILE\"))\n"
       "        shr->ca_file = av_strdup(getenv(\"SSL_CERT_FILE\"));\n"
       "    if (shr->ca_file) {")
assert s.count(old) == 1, "tls_mbedtls.c changed; update this patch"
s = s.replace(old, new)
if "#include <stdlib.h>" not in s:
    s = s.replace("#include <mbedtls/version.h>", "#include <stdlib.h>\n#include <mbedtls/version.h>", 1)
open(p, "w").write(s)
PY
fi
PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig" PKG_CONFIG_PATH="" ./configure \
    --prefix="$WORK/ffmpeg-install" \
    --target-os=android --arch=aarch64 --cpu=armv8-a --enable-cross-compile \
    --cc="$CC" --cxx="$CXX" --ar="$TC/bin/llvm-ar" --nm="$TC/bin/llvm-nm" \
    --ranlib="$TC/bin/llvm-ranlib" --strip="$TC/bin/llvm-strip" \
    --sysroot="$TC/sysroot" --pkg-config=pkg-config --pkg-config-flags=--static \
    --enable-static --disable-shared --enable-pic \
    --disable-doc --disable-ffplay --disable-avdevice --disable-indevs --disable-outdevs \
    --disable-debug --enable-version3 \
    --enable-libsvtav1 --enable-libopus --enable-mbedtls \
    --extra-cflags="-I$PREFIX/include -O2" \
    --extra-ldflags="-L$PREFIX/lib $([ "$LINK" = static ] && echo -static)" \
    --extra-libs="-lm"
make clean >/dev/null 2>&1 || true
make -j "$JOBS"
"$TC/bin/llvm-strip" ffmpeg ffprobe

[ "$LINK" = static ] && OUT="$WORK/static"
mkdir -p "$OUT"
cp ffmpeg "$OUT/libffmpeg.so"
cp ffprobe "$OUT/libffprobe.so"
ls -la "$OUT"
echo "== done: FFmpeg $FFMPEG_VER, SVT-AV1 $SVT_VER, Opus $OPUS_VER, mbedTLS $MBEDTLS_VER"
