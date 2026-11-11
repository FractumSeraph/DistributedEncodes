# =============================================================================
# Reproducible FFmpeg -> WebAssembly build for the Fractum browser worker.
#
# Produces static/ffmpeg.js, ffmpeg.wasm, ffmpeg.worker.js with:
#   - SVT-AV1 (libsvtav1) video encoding
#   - libopus audio encoding
#   - pthreads (needs COOP/COEP on the manager, already set)
#
# This mirrors the ORIGINAL build's FFmpeg configure line (recovered from the
# shipped ffmpeg.wasm, which was FFmpeg 6.1 / Lavc60.31.102) and bumps the
# components to current releases. Pin the ARG versions below to control it.
#
# Build:   docker build -t fractum-ffwasm ./wasm-build
# Extract: docker run --rm -v "$PWD/static:/out" fractum-ffwasm cp -a \
#            /build/dist/ffmpeg.js /build/dist/ffmpeg.wasm /build/dist/ffmpeg.worker.js /out/
#
# The same steps (same versions and flags) were run natively with emsdk 3.1.51
# to produce the files in static/. The docker build itself has not been run;
# see wasm-build/README.md for the pitfalls each workaround below addresses.
# =============================================================================

# Emscripten toolchain. 3.1.51 is a known-good era that still emits a separate
# <name>.worker.js for -pthread builds, which is what web_node.js importScripts.
# If you bump this and the separate .worker.js disappears, see the README.
ARG EMSDK_VERSION=3.1.51
FROM emscripten/emsdk:${EMSDK_VERSION}

# Component versions — bump these to rebuild with newer AV1 development.
ARG FFMPEG_VERSION=n7.1
ARG SVTAV1_VERSION=v2.3.0
ARG OPUS_VERSION=v1.5.2

ENV BUILD=/build
ENV PREFIX=/build/prefix
# -pthread everywhere; -fno-stack-protector avoids needing the old dummy_ssp.o
# stub the original build carried in its ldflags.
ENV CFLAGS="-O2 -pthread -fno-stack-protector -I${PREFIX}/include"
ENV CXXFLAGS="${CFLAGS}"
ENV LDFLAGS="-O2 -pthread -L${PREFIX}/lib"
ENV PKG_CONFIG_PATH="${PREFIX}/lib/pkgconfig"
# emconfigure points pkg-config at Emscripten's own sysroot and ignores
# PKG_CONFIG_PATH; this is the variable it honours. Without it FFmpeg's
# configure cannot find the opus / SVT-AV1 built below.
ENV EM_PKG_CONFIG_PATH="${PREFIX}/lib/pkgconfig"

RUN mkdir -p ${BUILD} ${PREFIX}
WORKDIR ${BUILD}

RUN apt-get update && apt-get install -y --no-install-recommends \
      git cmake ninja-build build-essential pkg-config yasm \
      autoconf automake libtool && \
    rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# 1. libopus  (audio encoder)
# -----------------------------------------------------------------------------
RUN git clone --depth 1 --branch ${OPUS_VERSION} https://github.com/xiph/opus.git && \
    cd opus && ./autogen.sh && \
    emconfigure ./configure --prefix=${PREFIX} --disable-shared --enable-static \
      --disable-doc --disable-extra-programs \
      --disable-asm --disable-rtcd --disable-intrinsics \
      --disable-stack-protector && \
    emmake make -j"$(nproc)" && emmake make install

# -----------------------------------------------------------------------------
# 2. SVT-AV1  (video encoder)  <-- the trickiest part; see README if it fails.
#    On a non-x86 toolchain SVT-AV1's CMake compiles its C-only path (no asm),
#    which is what we want for wasm. Apps/tests are off; threads use pthreads.
# -----------------------------------------------------------------------------
RUN git clone --depth 1 --branch ${SVTAV1_VERSION} https://gitlab.com/AOMediaCodec/SVT-AV1.git && \
    cd SVT-AV1 && mkdir b && cd b && \
    emcmake cmake .. -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF \
      -DBUILD_APPS=OFF \
      -DBUILD_TESTING=OFF \
      -DCMAKE_INSTALL_PREFIX=${PREFIX} \
      -DCMAKE_C_FLAGS="${CFLAGS}" \
      -DCMAKE_CXX_FLAGS="${CXXFLAGS}" && \
    ninja -j"$(nproc)" && ninja install

# -----------------------------------------------------------------------------
# 2b. Stack-protector stub. SVT-AV1's CMake always adds -fstack-protector-strong
#     and Emscripten ships no libssp, so the link needs these two symbols.
# -----------------------------------------------------------------------------
RUN printf '%s\n' '#include <stdint.h>' '#include <stdlib.h>' \
      'uintptr_t __stack_chk_guard = (uintptr_t)0x595e9fbdu;' \
      'void __stack_chk_fail(void) { abort(); }' > ssp_stub.c && \
    emcc -O2 -pthread -c ssp_stub.c -o ssp_stub.o && \
    emar rcs ${PREFIX}/lib/libssp_stub.a ssp_stub.o

# -----------------------------------------------------------------------------
# 3. FFmpeg  (mirrors the original configure line, components bumped)
#    The sed: FFmpeg 7.x treats any clang that accepts "-?" as clang-cl and
#    switches to MSVC flags (-nologo, -Fo). emcc accepts "-?", so drop the probe.
#    `make` ends by linking a plain ffmpeg_g, which fails under Emscripten
#    ("initial memory too small"); only the objects and archives are needed.
# -----------------------------------------------------------------------------
RUN git clone --depth 1 --branch ${FFMPEG_VERSION} https://git.ffmpeg.org/ffmpeg.git && \
    cd ffmpeg && \
    sed -i 's/ || { \$_cc -v 2>&1 | grep -q clang \&\& \$_cc -? > \/dev\/null 2>&1; }//' configure && \
    emconfigure ./configure \
      --prefix=${PREFIX} \
      --target-os=none \
      --arch=x86_32 \
      --enable-cross-compile \
      --disable-x86asm --disable-inline-asm \
      --disable-stripping \
      --enable-ffmpeg --disable-ffprobe --disable-ffplay --disable-doc \
      --disable-network --disable-debug --disable-autodetect \
      --disable-shared --enable-static \
      --enable-gpl \
      --enable-libsvtav1 --enable-encoder=libsvtav1 \
      --enable-libopus  --enable-encoder=libopus --enable-decoder=opus \
      --nm=emnm --ar=emar --ranlib=emranlib --cc=emcc --cxx=em++ \
      --extra-cflags="${CFLAGS}" --extra-cxxflags="${CXXFLAGS}" --extra-ldflags="${LDFLAGS}" \
      --extra-libs="-lssp_stub" \
      --pkg-config=pkg-config --pkg-config-flags="--static" && \
    (emmake make -j"$(nproc)" || true) && \
    ls fftools/ffmpeg.o libavcodec/libavcodec.a libavfilter/libavfilter.a libavformat/libavformat.a

# -----------------------------------------------------------------------------
# 4. Final Emscripten link into the browser-loadable ffmpeg.js/.wasm/.worker.js
#    ffmpeg's own build produces the object; we relink with the JS-runtime flags
#    web_node.js depends on (global Module, callMain, FS, pthreads, growth).
# -----------------------------------------------------------------------------
RUN mkdir -p ${BUILD}/dist && cd ${BUILD}/ffmpeg && \
    emcc -O2 -pthread \
      -o ${BUILD}/dist/ffmpeg.js \
      fftools/*.o libavcodec/*.a libavfilter/*.a libavformat/*.a \
      libavdevice/*.a libpostproc/*.a libavutil/*.a libswresample/*.a libswscale/*.a \
      ${PREFIX}/lib/libSvtAv1Enc.a ${PREFIX}/lib/libopus.a ${PREFIX}/lib/libssp_stub.a \
      -sUSE_PTHREADS=1 \
      -sPTHREAD_POOL_SIZE=8 \
      -sPROXY_TO_PTHREAD=1 \
      -sALLOW_MEMORY_GROWTH=1 \
      -sMAXIMUM_MEMORY=2gb \
      -sINITIAL_MEMORY=64mb \
      -sEXPORTED_RUNTIME_METHODS=callMain,FS \
      -sINVOKE_RUN=0 \
      -sEXIT_RUNTIME=0 \
      -sFORCE_FILESYSTEM=1 \
      -sMODULARIZE=0 \
      || (echo '*** Final link failed — see README "Linking" section ***' && false)

# The three artifacts to copy into ../static/
#   /build/dist/ffmpeg.js  /build/dist/ffmpeg.wasm  /build/dist/ffmpeg.worker.js
WORKDIR ${BUILD}/dist
