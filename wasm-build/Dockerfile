# =============================================================================
# FFmpeg -> WebAssembly build for the Fractum browser worker.
#
# Produces static/ffmpeg.js and static/ffmpeg.wasm with:
#   - SVT-AV1 video encoding, its x86 SSE/AVX2 intrinsics compiled to wasm SIMD
#   - libopus audio encoding
#   - pthreads (needs COOP/COEP on the manager, already set)
#
# Build:   docker build -t fractum-ffwasm ./wasm-build
# Extract: docker run --rm -v "$PWD/static:/out" fractum-ffwasm \
#            cp /build/dist/ffmpeg.js /build/dist/ffmpeg.wasm /out/
#
# All the steps live in build.sh, which is what was actually run (natively,
# with emsdk 4.0.10) to produce the files in static/. This Dockerfile only
# supplies the toolchain around it and has not itself been run.
#
# Keep Emscripten at 4.0.10 unless you re-verify the output (see README):
# 3.1.x has no AVX2 intrinsics, and 7.0.0 miscompiles SVT-AV1.
# =============================================================================
FROM emscripten/emsdk:4.0.10

RUN apt-get update && apt-get install -y --no-install-recommends \
      git cmake ninja-build build-essential pkg-config python3 \
      autoconf automake libtool && \
    rm -rf /var/lib/apt/lists/*

# Component versions. The SVT-AV1 patch in svt-wasm-simd/ is written for v2.3.0.
ENV FFMPEG_VERSION=n7.1 SVTAV1_VERSION=v2.3.0 OPUS_VERSION=v1.5.2
ENV BUILD=/build

COPY build.sh /wasm-build/build.sh
COPY svt-wasm-simd /wasm-build/svt-wasm-simd
RUN bash /wasm-build/build.sh

WORKDIR /build/dist
