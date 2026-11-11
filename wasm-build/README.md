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

## Why the previous build could not finish an encode

The `ffmpeg.wasm` shipped before this rebuild (FFmpeg 6.1 + **SVT-AV1 v1.8.0**) started
encoding and then died with `RuntimeError: unreachable` as soon as the first
frame reached SVT-AV1's picture manager. It never produced output, for any
input size.

Cause: in SVT-AV1 1.8.0, `rtime_alloc_palette_tokens()` is *declared* in
`EbPictureManagerProcess.c` as returning `void` but *defined* in
`EbPictureControlSet.c` as returning `EbErrorType`. Native compilers tolerate
that. WebAssembly checks call signatures, so the linker replaces the call with
a stub that traps. (In the shipped binary: function 50, a bare `unreachable`,
called once from the picture manager kernel.)

Upstream fixed it in **v2.0.0** (the function became `static` with one
signature), so any build from SVT-AV1 2.x or later avoids it. The Dockerfile
here pins v2.3.0. When linking, treat any `function signature mismatch`
warning from `wasm-ld` as a build failure: each one is a guaranteed trap at
run time.

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

## What the build needed (all already in the Dockerfile)

The files now in `static/` were produced by running the Dockerfile's steps
natively with emsdk 3.1.51 (FFmpeg n7.1, SVT-AV1 v2.3.0, opus v1.5.2). The
recipe as first written failed at four points; each has a workaround in place:

1. **FFmpeg 7.x mistakes `emcc` for clang-cl.** Its configure treats any clang
   that accepts `-?` as clang-cl and switches to MSVC flags (`-nologo`, `-Fo`).
   A `sed` drops that probe.
2. **pkg-config cannot see the libraries just built.** `emconfigure` points
   pkg-config at Emscripten's sysroot and ignores `PKG_CONFIG_PATH`; it honours
   `EM_PKG_CONFIG_PATH`.
3. **Stack protector.** opus and SVT-AV1 both turn on `-fstack-protector-strong`
   and Emscripten has no libssp. opus takes `--disable-stack-protector`;
   SVT-AV1 has no switch, so a two-symbol stub library is linked in.
4. **Final link.** `--enable-gpl` builds `libpostproc`, and `fftools` needs
   `libavdevice`; both must be on the link line. FFmpeg's own `make` ends by
   linking a plain `ffmpeg_g`, which fails under Emscripten ("initial memory too
   small") and is ignored: only the objects and archives are used.

`web_node.js` expects a separate `ffmpeg.worker.js`. Emscripten 3.1.51 emits
one; newer versions may not, in which case pin the version or update the worker
bootstrap in `web_node.js`.

## Known behaviour of this build

- **It never exits.** FFmpeg encodes, finalises and closes the output, prints
  its final report, then hangs in its own shutdown and never calls `exit`.
  `web_node.js` therefore treats `progress=end` in FFmpeg's `-progress` output
  as completion, and the page starts a fresh worker before the next job.
- **It is slow.** No SIMD and one encoder thread (`lp=1`). Measured in headless
  Chromium: about 0.26 frames per second at 640x480, preset 2 (300 frames in
  19 minutes), using roughly 570 MB.

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
