#!/usr/bin/env python3
"""Patch an SVT-AV1 v2.3.0 tree so its x86 SSE2..AVX2 *intrinsics* code builds
for WebAssembly SIMD with Emscripten (-msimd128 -mavx2).

Usage: patch_svt.py <path-to-SVT-AV1>

What it does:
  * treats the Emscripten target as an x86 platform (ARCH_X86_64=1) when
    -DSVT_WASM_SIMD=ON, without NASM, cpuinfo, tests or AVX-512;
  * skips every .asm source (hand-written x86 assembly cannot target wasm);
  * reports SSE2..AVX2 as the available CPU features, since there is no cpuid.
Functions whose only SIMD version is in a .asm file are redirected to their C
version by a generated header (see gen_asm_fallbacks.py).
"""
import sys, os, re

root = sys.argv[1]

def edit(rel, pairs):
    p = os.path.join(root, rel)
    s = open(p, encoding='utf-8').read()
    for old, new in pairs:
        if new in s:
            continue                      # already applied
        assert s.count(old) == 1, (rel, s.count(old), old[:70])
        s = s.replace(old, new)
    open(p, 'w', encoding='utf-8').write(s)

edit('CMakeLists.txt', [
    ('''option(MINIMAL_BUILD "Enable minimal build" OFF)''',
     '''option(SVT_WASM_SIMD "Emscripten: build the x86 intrinsics paths for wasm SIMD" OFF)
if(EMSCRIPTEN AND SVT_WASM_SIMD)
    set(HAVE_X86_PLATFORM 1)
    set(HAVE_ARM_PLATFORM 0)
endif()

option(MINIMAL_BUILD "Enable minimal build" OFF)'''),
    ('''if(NOT COMPILE_C_ONLY AND HAVE_X86_PLATFORM)
    include(CheckLanguage)
    check_language(ASM_NASM)''',
     '''if(EMSCRIPTEN AND SVT_WASM_SIMD)
    add_definitions(-DARCH_X86_64=1 -DSVT_WASM_SIMD=1)
elseif(NOT COMPILE_C_ONLY AND HAVE_X86_PLATFORM)
    include(CheckLanguage)
    check_language(ASM_NASM)'''),
    ('''if(NOT USE_EXTERNAL_CPUINFO AND NOT COMPILE_C_ONLY AND HAVE_X86_PLATFORM)
    add_subdirectory(third_party/cpuinfo)''',
     '''if(NOT USE_EXTERNAL_CPUINFO AND NOT COMPILE_C_ONLY AND HAVE_X86_PLATFORM AND NOT EMSCRIPTEN)
    add_subdirectory(third_party/cpuinfo)'''),
    ('''macro(ASM_COMPILE_TO_TARGET target)''',
     '''macro(ASM_COMPILE_TO_TARGET target)
    if(EMSCRIPTEN)
        return()
    endif()'''),
])

edit('Source/Lib/CMakeLists.txt', [
    ('''elseif(NOT COMPILE_C_ONLY AND HAVE_X86_PLATFORM)
    target_link_libraries(SvtAv1Enc PRIVATE cpuinfo_public)''',
     '''elseif(NOT COMPILE_C_ONLY AND HAVE_X86_PLATFORM AND NOT EMSCRIPTEN)
    target_link_libraries(SvtAv1Enc PRIVATE cpuinfo_public)'''),
])

edit('Source/Lib/Codec/common_dsp_rtcd.c', [
    ('''#include "cpuinfo.h"''',
     '''#ifndef SVT_WASM_SIMD
#include "cpuinfo.h"
#else
#include "wasm_simd_rtcd_fallbacks.h"
// MMX state does not exist in WebAssembly; the assembly version is not built.
void RunEmms(void) {}
#endif'''),
    ('''#ifdef ARCH_X86_64
EbCpuFlags svt_aom_get_cpu_flags() {
    EbCpuFlags flags = 0;
''',
     '''#if defined(SVT_WASM_SIMD)
// WebAssembly has no cpuid. The x86 intrinsics are compiled to wasm SIMD by
// Emscripten (-msimd128 -mavx2, 256-bit vectors emulated with two 128-bit
// ones), so everything up to AVX2 is always there.
EbCpuFlags svt_aom_get_cpu_flags() {
    return EB_CPU_FLAGS_MMX | EB_CPU_FLAGS_SSE | EB_CPU_FLAGS_SSE2 | EB_CPU_FLAGS_SSE3 |
        EB_CPU_FLAGS_SSSE3 | EB_CPU_FLAGS_SSE4_1 | EB_CPU_FLAGS_SSE4_2 | EB_CPU_FLAGS_AVX |
        EB_CPU_FLAGS_AVX2;
}

EbCpuFlags svt_aom_get_cpu_flags_to_use() { return svt_aom_get_cpu_flags(); }
#elif defined(ARCH_X86_64)
EbCpuFlags svt_aom_get_cpu_flags() {
    EbCpuFlags flags = 0;
'''),
])
# Hand-written support files: fast replacements for two slowly emulated
# intrinsics, and C versions of a few assembly-only filter helpers.
import shutil
here = os.path.dirname(os.path.abspath(__file__))
for f in ('wasm_simd_intrinsics.h', 'wasm_simd_asm_helpers.c'):
    shutil.copy(os.path.join(here, 'overlay', f), os.path.join(root, 'Source/Lib/Codec', f))
edit('Source/Lib/Codec/CMakeLists.txt', [
    ('''set(all_files
''', '''set(all_files
    wasm_simd_asm_helpers.c
'''),
])

# aom_dsp_rtcd.c: pull in the dispatch fallbacks after its last #include
p = os.path.join(root, 'Source/Lib/Codec/aom_dsp_rtcd.c')
t = open(p, encoding='utf-8').read()
if 'wasm_simd_rtcd_fallbacks.h' not in t:
    last = [m for m in re.finditer(r'^#include .*$', t, re.M)][-1]
    t = t[:last.end()] + '\n#ifdef SVT_WASM_SIMD\n#include "wasm_simd_rtcd_fallbacks.h"\n#endif' + t[last.end():]
    open(p, 'w', encoding='utf-8').write(t)
# The generated headers must exist even before gen_fallbacks.py has run.
for h in ('wasm_simd_asm_fallbacks.h', 'wasm_simd_rtcd_fallbacks.h'):
    hp = os.path.join(root, 'Source/Lib/Codec', h)
    if not os.path.exists(hp):
        open(hp, 'w').write('/* placeholder; regenerate with gen_fallbacks.py */\n')
print("patched", root)
