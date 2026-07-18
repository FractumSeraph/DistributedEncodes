# Rebuilding the browser worker's FFmpeg WebAssembly

The in-browser worker (`/web`) runs a custom FFmpeg compiled to WebAssembly with
**SVT-AV1** and **libopus**. This directory rebuilds it reproducibly with Docker,
so you can pick up newer AV1 development without remembering the original steps.

## What the original build was

Recovered from the shipped `static/ffmpeg.wasm`:

- **FFmpeg 6.1** (`Lavc60.31.102`)
- Emscripten toolchain, `--arch=x86_32` (32-bit), `-pthread`
- `--enable-gpl --enable-libsvtav1`, libopus statically linked
- `--disable-network --disable-ffprobe --disable-ffplay --disable-doc`
- Output: `ffmpeg.js` + `ffmpeg.wasm` + `ffmpeg.worker.js` (classic pthread build,
  non-modularized — `web_node.js` does `importScripts('ffmpeg.js')` and uses the
  global `self.Module`).

The `Dockerfile` here reproduces that configuration and bumps the components to
current releases (FFmpeg 7.1, SVT-AV1 2.3.0, opus 1.5.2 by default — change the
`ARG` lines to pin whatever you want).

## Build it

```bash
# from the repo root
docker build -t fractum-ffwasm ./wasm-build

# copy the three artifacts into static/ (overwrites the old wasm)
docker run --rm -v "$PWD/static:/out" fractum-ffwasm \
  sh -c 'cp -a /build/dist/ffmpeg.js /build/dist/ffmpeg.wasm /build/dist/ffmpeg.worker.js /out/'
```

Then hard-refresh `/web` (the page already cache-busts the worker URL) and run a
small test file through it.

## Bumping versions

Edit the `ARG` lines at the top of the `Dockerfile`:

```dockerfile
ARG FFMPEG_VERSION=n7.1
ARG SVTAV1_VERSION=v2.3.0
ARG OPUS_VERSION=v1.5.2
ARG EMSDK_VERSION=3.1.51
```

SVT-AV1 has moved a lot since your original build — newer releases encode faster
at the same quality, so a bump is worthwhile.

## Two spots most likely to need a tweak (be ready to iterate)

This recipe reconstructs your exact FFmpeg configure line (that part is known-good
from your binary), but two steps depend on toolchain/library specifics that shift
between versions. If a `docker build` fails, it's almost certainly one of these:

1. **SVT-AV1's wasm CMake (step 2).** SVT-AV1 is heavily x86-optimized; on the
   Emscripten toolchain its CMake should fall back to the portable C path
   automatically (no asm). If it tries to use x86 intrinsics or fails detecting
   the target, pass `-DCMAKE_SYSTEM_PROCESSOR=generic` and/or the release's
   documented "C-only" switch. SVT-AV1 also leans on threads heavily — keep the
   Emscripten `PTHREAD_POOL_SIZE` (step 4) at least as large as the encoder's
   thread count (the browser worker already caps SVT threads via
   `-svtav1-params lp=1`, so a small pool is fine).

2. **Emscripten pthread output naming (steps 1/4).** `web_node.js` expects a
   separate `ffmpeg.worker.js`. Emscripten versions around **3.1.51** emit that
   file for `-pthread`; some **newer** versions changed how the pthread worker is
   delivered and may not emit a standalone `.worker.js`. If yours doesn't:
   either pin `EMSDK_VERSION` back to a version that does, **or** update
   `web_node.js` (the `mainScriptUrlOrBlob` / worker bootstrap) to the new model.
   Nothing else in the app cares which you choose.

## Memory reality (why this only takes small files)

The build is 32-bit (`--arch=x86_32`), so the whole process is capped near 2 GB
usable, and `--disable-network` means the page must load its input into memory
before encoding.

For **whole-file** browser jobs that limits you to small sources (the `/web`
page's "MAX SOURCE MB", default 150). For **large** sources, the browser worker
uses **server-side segmentation** (implemented): it takes one chunk at a time
and downloads only that chunk's small pre-cut segment via `/download_segment`,
so it never loads a multi-GB file. See the "Server-side segmentation" section in
the main README. That means a newer wasm doesn't need memory64 to help on big
files — segmentation already keeps each piece small.
