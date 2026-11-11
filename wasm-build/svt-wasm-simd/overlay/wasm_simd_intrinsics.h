/*
 * Faster WebAssembly-SIMD versions of two x86 intrinsics that Emscripten
 * emulates with scalar loops. SVT-AV1's motion search (the SAD kernels) is
 * built on them, so the emulation dominated the whole encode.
 *
 * Both replacements are exact: they return the same bits as the instruction.
 */
#ifndef WASM_SIMD_INTRINSICS_H
#define WASM_SIMD_INTRINSICS_H
#if defined(SVT_WASM_SIMD) && defined(__EMSCRIPTEN__) && !defined(__ASSEMBLER__)

#include <wasm_simd128.h>
#include <immintrin.h>

/* Bytes [n .. n+15] of v, zero filled past the end (n is 0..7 here). */
static inline __attribute__((always_inline)) v128_t svt_wasm_shr_bytes(v128_t v, int n) {
    const v128_t z = wasm_i64x2_const(0, 0);
    switch (n) {
    case 0: return v;
    case 1: return wasm_i8x16_shuffle(v, z, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16);
    case 2: return wasm_i8x16_shuffle(v, z, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 16);
    case 3: return wasm_i8x16_shuffle(v, z, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 16, 16);
    case 4: return wasm_i8x16_shuffle(v, z, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 16, 16, 16);
    case 5: return wasm_i8x16_shuffle(v, z, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 16, 16, 16, 16);
    case 6: return wasm_i8x16_shuffle(v, z, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 16, 16, 16, 16, 16);
    default: return wasm_i8x16_shuffle(v, z, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 16, 16, 16, 16, 16, 16);
    }
}

/* b[lane] repeated in every byte (lane is 0..15). */
static inline __attribute__((always_inline)) v128_t svt_wasm_splat_byte(v128_t b, int lane) {
    switch (lane) {
    case 0: return wasm_i8x16_shuffle(b, b, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
    case 1: return wasm_i8x16_shuffle(b, b, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1);
    case 2: return wasm_i8x16_shuffle(b, b, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2);
    case 3: return wasm_i8x16_shuffle(b, b, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3);
    case 4: return wasm_i8x16_shuffle(b, b, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4);
    case 5: return wasm_i8x16_shuffle(b, b, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5);
    case 6: return wasm_i8x16_shuffle(b, b, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6);
    case 7: return wasm_i8x16_shuffle(b, b, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7);
    case 8: return wasm_i8x16_shuffle(b, b, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8);
    case 9: return wasm_i8x16_shuffle(b, b, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9);
    case 10: return wasm_i8x16_shuffle(b, b, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10);
    case 11: return wasm_i8x16_shuffle(b, b, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11);
    case 12: return wasm_i8x16_shuffle(b, b, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12);
    case 13: return wasm_i8x16_shuffle(b, b, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13);
    case 14: return wasm_i8x16_shuffle(b, b, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14);
    default: return wasm_i8x16_shuffle(b, b, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15);
    }
}

/* MPSADBW: for i in 0..7,
 *   r[i] = sum over k in 0..3 of |a[i + aoff + k] - b[boff + k]|
 * with aoff = imm & 4 and boff = (imm & 3) * 4. */
static inline __attribute__((always_inline)) __m128i svt_wasm_mpsadbw_epu8(__m128i a, __m128i b, int imm8) {
    const int aoff = imm8 & 4;
    const int boff = (imm8 & 3) << 2;
    v128_t acc = wasm_i64x2_const(0, 0);
    for (int k = 0; k < 4; ++k) {
        const v128_t av = svt_wasm_shr_bytes((v128_t)a, aoff + k);
        const v128_t bv = svt_wasm_splat_byte((v128_t)b, boff + k);
        /* |x - y| for unsigned bytes */
        const v128_t d = wasm_v128_or(wasm_u8x16_sub_sat(av, bv), wasm_u8x16_sub_sat(bv, av));
        acc = wasm_i16x8_add(acc, wasm_u16x8_extend_low_u8x16(d));
    }
    return (__m128i)acc;
}

/* PHMINPOSUW: lane 0 = smallest of the eight u16, lane 1 = its lowest index. */
static inline __attribute__((always_inline)) __m128i svt_wasm_minpos_epu16(__m128i a) {
    v128_t m = wasm_u16x8_min((v128_t)a, wasm_i16x8_shuffle((v128_t)a, (v128_t)a, 4, 5, 6, 7, 0, 1, 2, 3));
    m = wasm_u16x8_min(m, wasm_i16x8_shuffle(m, m, 2, 3, 0, 1, 6, 7, 4, 5));
    m = wasm_u16x8_min(m, wasm_i16x8_shuffle(m, m, 1, 0, 3, 2, 5, 4, 7, 6));
    const unsigned mask = wasm_i16x8_bitmask(wasm_i16x8_eq((v128_t)a, m));   /* never zero */
    const unsigned idx = (unsigned)__builtin_ctz(mask);
    const unsigned minv = (unsigned)wasm_u16x8_extract_lane(m, 0);
    return (__m128i)wasm_i32x4_make((int)(minv | (idx << 16)), 0, 0, 0);
}

static inline __attribute__((always_inline)) __m256i svt_wasm_mm256_mpsadbw_epu8(__m256i a, __m256i b, int imm8) {
    /* Each 128-bit half uses its own 3-bit control: bits 0-2 low, bits 3-5 high. */
    const __m128i lo = svt_wasm_mpsadbw_epu8(_mm256_castsi256_si128(a), _mm256_castsi256_si128(b), imm8 & 7);
    const __m128i hi = svt_wasm_mpsadbw_epu8(_mm256_extracti128_si256(a, 1), _mm256_extracti128_si256(b, 1),
                                             (imm8 >> 3) & 7);
    return _mm256_set_m128i(hi, lo);
}

#undef _mm_mpsadbw_epu8
#undef _mm256_mpsadbw_epu8
#undef _mm_minpos_epu16
#define _mm_mpsadbw_epu8(a, b, imm8) svt_wasm_mpsadbw_epu8((a), (b), (imm8))
#define _mm256_mpsadbw_epu8(a, b, imm8) svt_wasm_mm256_mpsadbw_epu8((a), (b), (imm8))
#define _mm_minpos_epu16(a) svt_wasm_minpos_epu16((a))

#endif
#endif
