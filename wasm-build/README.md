# The browser worker's FFmpeg WebAssembly build

The in-browser worker (`/web`) runs FFmpeg compiled to WebAssembly with
**SVT-AV1** and **libopus**. `static/ffmpeg.js` and `static/ffmpeg.wasm` are
built by `build.sh` in this directory:

- FFmpeg n7.1, SVT-AV1 v2.3.0, opus v1.5.2
- Emscripten **4.0.10**
- SVT-AV1's x86 SSE2–AVX2 *intrinsics* compiled to WebAssembly SIMD

Its output is byte-identical to a native build of SVT-AV1 2.3.0 with the same
settings (checked on two clips at preset 2, CRF 63, one thread).

## Build it

With Docker:

```bash
docker build -t fractum-ffwasm ./wasm-build
docker run --rm -v "$PWD/static:/out" fractum-ffwasm \
  cp /build/dist/ffmpeg.js /build/dist/ffmpeg.wasm /out/
```

Or natively, with emsdk 4.0.10 active (`source emsdk_env.sh`):

```bash
BUILD=/some/scratch/dir bash wasm-build/build.sh
cp /some/scratch/dir/dist/ffmpeg.js /some/scratch/dir/dist/ffmpeg.wasm static/
```

`build.sh` is what produced the committed files (run natively). The Dockerfile
wraps the same script and has not itself been run.

## How SVT-AV1 gets its SIMD (`svt-wasm-simd/`)

SVT-AV1 is slow without vector code: built as plain C it encodes at about a
quarter of the speed below. Its vector code is x86-only, in two forms:

- **intrinsics** (C files using `_mm_*` / `_mm256_*`), which Emscripten can
  compile to WebAssembly SIMD with `-msimd128 -mavx2` (256-bit vectors are
  emulated with two 128-bit ones);
- **hand-written assembly** (`.asm`), which cannot be built for WebAssembly.

`patch_svt.py` makes the Emscripten build take the x86 path: it defines
`ARCH_X86_64`, skips NASM, cpuinfo and AVX-512, and reports SSE2–AVX2 as
available (there is no `cpuid`).

`gen_fallbacks.py` deals with the assembly. After a first compile it reads the
library's symbol table and writes two headers:

- `wasm_simd_asm_fallbacks.h` (force-included everywhere): functions that
  exist *only* as assembly are renamed to their C version.
- `wasm_simd_rtcd_fallbacks.h` (included by the two run-time dispatch files):
  intrinsics wrappers that call assembly helpers are replaced, in the dispatch
  tables, by SVT-AV1's own non-assembly implementation. This includes the
  dav1d inverse transforms.

`overlay/` holds the hand-written parts:

- `wasm_simd_intrinsics.h`: exact WebAssembly-SIMD versions of `MPSADBW` and
  `PHMINPOSUW`. Emscripten emulates both with scalar loops, and SVT-AV1's
  motion search is built on them: with the stock emulation one SAD kernel was
  about 70% of all compute.
- `wasm_simd_asm_helpers.c`: C versions of seven small filter helpers that
  only exist as assembly, so the AVX2 convolve can be used.

One AVX2 function, `svt_copy_mi_map_grid_avx2`, gives a different result under
Emscripten's 256-bit emulation and corrupts the bitstream. It was found by
switching each of the 542 AVX2 dispatch entries on and off against the C
build's output, and is redirected to its C version.

### Measured (standalone encoder under Node, 640x480, preset 2, CRF 63, lp=1)

| Build                                   | Speed    |
|-----------------------------------------|----------|
| Plain C                                 | 0.24 fps |
| SIMD, stock Emscripten intrinsics       | 0.30 fps |
| SIMD + fast MPSADBW / PHMINPOSUW        | 0.87 fps |
| SIMD + those + AVX2 convolve (shipped)  | 1.02 fps |
| Native, same version, AVX-512, lp=1     | 3.89 fps |

All four WebAssembly rows produce the same bytes as the native build.

## If you change versions

- **Emscripten.** 3.1.x has no AVX2 intrinsics. **7.0.0 miscompiles SVT-AV1 at
  `-O2` and above** even as plain C: only the first frame decodes. After any
  toolchain change, encode a clip with the standalone encoder and compare its
  md5 against a native build of the same SVT-AV1 version.
- **SVT-AV1.** The patch anchors and the fallback lists are for v2.3.0. A new
  version needs `gen_fallbacks.py` re-run and the bisection repeated if the
  output stops matching. v1.8.0 and earlier cannot work at all: they declare
  `rtime_alloc_palette_tokens()` with two different return types, which
  WebAssembly turns into a trap on the first frame (fixed upstream in v2.0.0).
  Treat any `function signature mismatch` warning from `wasm-ld` as a failure.
- **FFmpeg 7.x build workarounds** already in `build.sh`: configure mistakes
  `emcc` for clang-cl (a `sed` drops that probe); `emconfigure` needs
  `EM_PKG_CONFIG_PATH`; opus and SVT-AV1 enable the stack protector, which
  Emscripten has no library for (opus is built without it, SVT-AV1 gets a
  two-symbol stub); `libavdevice` and `libpostproc` must be on the final link
  line; FFmpeg's own `ffmpeg_g` link fails and is ignored.

## Known behaviour of this build

- **It never exits.** FFmpeg encodes, finalises and closes the output, prints
  its final report, then hangs in its own shutdown and never calls `exit`.
  `web_node.js` treats `progress=end` in FFmpeg's `-progress` output as
  completion, and the page starts a fresh worker before the next job.
- **Packets carry no duration.** Without help the MP4 comes out one frame
  short, so `web_node.js` adds `-bsf:v setts=duration=1` to chunk encodes.
- **Still slower than native.** One encoder thread (`lp=1`), emulated 256-bit
  vectors, and no assembly: roughly a quarter of native single-thread speed.

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
