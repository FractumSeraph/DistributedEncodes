#!/bin/bash
# Builds static/ffmpeg.js + ffmpeg.wasm for the browser worker:
# FFmpeg + SVT-AV1 (x86 intrinsics compiled to WebAssembly SIMD) + libopus.
#
# Needs Emscripten 4.0.10 on PATH (the Dockerfile's base image, or
# "source emsdk_env.sh" after "emsdk install 4.0.10 && emsdk activate 4.0.10"),
# plus git, cmake, ninja, pkg-config, autoconf, automake, libtool, python3.
#
#   BUILD=/some/dir bash wasm-build/build.sh
#   cp $BUILD/dist/ffmpeg.js $BUILD/dist/ffmpeg.wasm static/
#
# Do not use a newer Emscripten without re-checking the output: 7.0.0
# miscompiles SVT-AV1 at -O2 and above (only the first frame decodes).
set -eo pipefail
WB="$(cd "$(dirname "$0")" && pwd)"
FFMPEG_VERSION="${FFMPEG_VERSION:-n7.1}"
SVTAV1_VERSION="${SVTAV1_VERSION:-v2.3.0}"
OPUS_VERSION="${OPUS_VERSION:-v1.5.2}"
BUILD="${BUILD:-$WB/build}"; PREFIX="$BUILD/prefix"
mkdir -p "$BUILD" "$PREFIX"
emcc --version | head -1

export CFLAGS="-O2 -pthread -fno-stack-protector -I${PREFIX}/include"
export CXXFLAGS="${CFLAGS}"
export LDFLAGS="-O2 -pthread -L${PREFIX}/lib"
export PKG_CONFIG_PATH="${PREFIX}/lib/pkgconfig"
# emconfigure points pkg-config at its own sysroot; this is the variable it honours.
export EM_PKG_CONFIG_PATH="${PREFIX}/lib/pkgconfig"
J=$(nproc)

cd "$BUILD"
if [ ! -f "$PREFIX/.opus_ok" ]; then
  echo "=== opus ==="
  [ -d opus ] || git clone -q --depth 1 --branch "$OPUS_VERSION" https://github.com/xiph/opus.git
  cd opus && ./autogen.sh >/dev/null 2>&1
  emconfigure ./configure --prefix="$PREFIX" --disable-shared --enable-static \
    --disable-doc --disable-extra-programs --disable-asm --disable-rtcd --disable-intrinsics \
    --disable-stack-protector >/dev/null
  emmake make clean >/dev/null 2>&1 || true
  emmake make -j"$J" >/dev/null && emmake make install >/dev/null
  touch "$PREFIX/.opus_ok"
  cd "$BUILD"
fi

if [ ! -f "$PREFIX/lib/libSvtAv1Enc.a" ]; then
  echo "=== svt-av1 (wasm SIMD) ==="
  SIMD="$WB/svt-wasm-simd"
  [ -d SVT-AV1 ] || git clone -q --depth 1 --branch "$SVTAV1_VERSION" https://gitlab.com/AOMediaCodec/SVT-AV1.git
  python3 "$SIMD/patch_svt.py" SVT-AV1
  SVTFLAGS="-O3 -pthread -msimd128 -mavx2 -fno-stack-protector"
  svt_cfg() { emcmake cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF       -DBUILD_APPS=OFF -DBUILD_TESTING=OFF -DSVT_WASM_SIMD=ON -DENABLE_AVX512=OFF -DSVT_AV1_LTO=OFF       -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_C_FLAGS="$1" -DCMAKE_CXX_FLAGS="$SVTFLAGS" >/dev/null; }
  # Pass 1: compile everything, to learn which functions exist only as assembly.
  cd SVT-AV1 && mkdir -p b1 && cd b1 && svt_cfg "$SVTFLAGS" && ninja -j"$J" >/dev/null
  cp ../Bin/Release/libSvtAv1Enc.a "$BUILD/svt_pass1.a"
  python3 "$SIMD/gen_fallbacks.py" "$BUILD/SVT-AV1" "$BUILD/svt_pass1.a" emnm
  # Pass 2: the real build, with those functions redirected.
  cd .. && mkdir -p b2 && cd b2 &&     svt_cfg "$SVTFLAGS -include $BUILD/SVT-AV1/Source/Lib/Codec/wasm_simd_asm_fallbacks.h" &&     ninja -j"$J" >/dev/null && ninja install >/dev/null
  cd "$BUILD"
fi

# SVT-AV1's CMake always adds -fstack-protector-strong, and Emscripten ships
# no libssp. Provide the two symbols it needs (the original build carried the
# same kind of stub).
if [ ! -f "$PREFIX/lib/libssp_stub.a" ]; then
  cat > "$BUILD/ssp_stub.c" <<'EOC'
#include <stdint.h>
#include <stdlib.h>
uintptr_t __stack_chk_guard = (uintptr_t)0x595e9fbdu;
void __stack_chk_fail(void) { abort(); }
EOC
  emcc -O2 -pthread -c "$BUILD/ssp_stub.c" -o "$BUILD/ssp_stub.o"
  emar rcs "$PREFIX/lib/libssp_stub.a" "$BUILD/ssp_stub.o"
fi

if [ ! -f "$BUILD/ffmpeg/ffmpeg_g" ] && [ ! -f "$BUILD/ffmpeg/.built" ]; then
  echo "=== ffmpeg ==="
  [ -d ffmpeg ] || git clone -q --depth 1 --branch "$FFMPEG_VERSION" https://git.ffmpeg.org/ffmpeg.git
  cd ffmpeg
  # FFmpeg 7.x takes any clang that accepts "-?" for clang-cl and switches to
  # MSVC-style flags (-nologo, -Fo). emcc accepts "-?", so drop that probe.
  sed -i 's/ || { \$_cc -v 2>&1 | grep -q clang \&\& \$_cc -? > \/dev\/null 2>&1; }//' configure
  emconfigure ./configure --prefix="$PREFIX" --target-os=none --arch=x86_32 \
    --enable-cross-compile --disable-x86asm --disable-inline-asm --disable-stripping \
    --enable-ffmpeg --disable-ffprobe --disable-ffplay --disable-doc \
    --disable-network --disable-debug --disable-autodetect \
    --disable-shared --enable-static --enable-gpl \
    --enable-libsvtav1 --enable-encoder=libsvtav1 \
    --enable-libopus --enable-encoder=libopus --enable-decoder=opus \
    --nm=emnm --ar=emar --ranlib=emranlib --cc=emcc --cxx=em++ \
    --extra-cflags="$CFLAGS" --extra-cxxflags="$CXXFLAGS" --extra-ldflags="$LDFLAGS" \
    --extra-libs="-lssp_stub" \
    --pkg-config=pkg-config --pkg-config-flags="--static" > configure.out 2>&1 || { tail -30 configure.out; tail -40 ffbuild/config.log; exit 1; }
  # The program link at the end of `make` may fail under emscripten; the
  # objects and archives are what the final link below needs.
  emmake make -j"$J" > make.out 2>&1 || { echo "make exited non-zero (checking objects)"; tail -15 make.out; }
  ls fftools/*.o libavcodec/libavcodec.a >/dev/null
  touch .built
  cd "$BUILD"
fi

echo "=== link ==="
mkdir -p "$BUILD/dist" && cd "$BUILD/ffmpeg"
emcc -O2 -pthread -o "$BUILD/dist/ffmpeg.js" \
  fftools/*.o libavcodec/*.a libavfilter/*.a libavformat/*.a \
  libavdevice/*.a libpostproc/*.a libavutil/*.a libswresample/*.a libswscale/*.a \
  "$PREFIX/lib/libSvtAv1Enc.a" "$PREFIX/lib/libopus.a" "$PREFIX/lib/libssp_stub.a" \
  -sUSE_PTHREADS=1 -sPTHREAD_POOL_SIZE=8 -sPROXY_TO_PTHREAD=1 \
  -sALLOW_MEMORY_GROWTH=1 -sMAXIMUM_MEMORY=4gb -sINITIAL_MEMORY=1gb \
  -sSTACK_SIZE=5mb -sDEFAULT_PTHREAD_STACK_SIZE=2mb \
  -sEXPORTED_RUNTIME_METHODS=callMain,FS -sINVOKE_RUN=0 -sEXIT_RUNTIME=0 \
  -sFORCE_FILESYSTEM=1 -sMODULARIZE=0
ls -la "$BUILD/dist"
echo "=== done ==="
