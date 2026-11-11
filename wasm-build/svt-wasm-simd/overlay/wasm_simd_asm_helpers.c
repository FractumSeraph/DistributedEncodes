/*
 * C versions of the few 1-D filter helpers that SVT-AV1 only has as x86
 * assembly and that its AVX2 convolve wrapper (aom_subpixel_8t_intrin_avx2.c)
 * calls for the 4-wide 8-tap vertical case and for 2-tap (bilinear) filters.
 * With these, the wrapper can be used under WebAssembly instead of falling
 * back to the plain C convolve for every block size.
 *
 * Arithmetic is the same as svt_aom_convolve8_{horiz,vert}_c:
 * round(sum * 2^-7), clipped to 8 bits.
 */
#ifdef SVT_WASM_SIMD
#include <stddef.h>
#include <stdint.h>

static inline uint8_t clip8(int v) { return (uint8_t)(v < 0 ? 0 : (v > 255 ? 255 : v)); }

/* 8-tap vertical, 4 wide. src_ptr is already 3 rows above the output row. */
void svt_aom_filter_block1d4_v8_sse2(const uint8_t *src_ptr, ptrdiff_t src_pitch, uint8_t *output_ptr,
                                     ptrdiff_t out_pitch, uint32_t output_height, const int16_t *filter) {
    for (uint32_t y = 0; y < output_height; ++y) {
        for (int x = 0; x < 4; ++x) {
            int sum = 0;
            for (int k = 0; k < 8; ++k) sum += src_ptr[x + k * src_pitch] * filter[k];
            output_ptr[x] = clip8((sum + 64) >> 7);
        }
        src_ptr += src_pitch;
        output_ptr += out_pitch;
    }
}

/* 2-tap: only filter[3] and filter[4] are non-zero. src_ptr is the output position. */
#define BILINEAR(name, W, STEP)                                                                          \
    void name(const uint8_t *src_ptr, ptrdiff_t src_pitch, uint8_t *output_ptr, ptrdiff_t out_pitch,     \
              uint32_t output_height, const int16_t *filter) {                                           \
        for (uint32_t y = 0; y < output_height; ++y) {                                                   \
            for (int x = 0; x < (W); ++x) {                                                              \
                const int sum = src_ptr[x] * filter[3] + src_ptr[x + (STEP)] * filter[4];                \
                output_ptr[x] = clip8((sum + 64) >> 7);                                                  \
            }                                                                                            \
            src_ptr += src_pitch;                                                                        \
            output_ptr += out_pitch;                                                                     \
        }                                                                                                \
    }

BILINEAR(svt_aom_filter_block1d16_h2_ssse3, 16, 1)
BILINEAR(svt_aom_filter_block1d8_h2_ssse3, 8, 1)
BILINEAR(svt_aom_filter_block1d4_h2_ssse3, 4, 1)
BILINEAR(svt_aom_filter_block1d16_v2_ssse3, 16, src_pitch)
BILINEAR(svt_aom_filter_block1d8_v2_ssse3, 8, src_pitch)
BILINEAR(svt_aom_filter_block1d4_v2_ssse3, 4, src_pitch)
#else
typedef int svt_wasm_simd_asm_helpers_unused;
#endif
