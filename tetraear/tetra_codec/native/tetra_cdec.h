/* Memory API for ETSI EN 300 395-2 channel decoder (replaces cdecoder.exe). */
#ifndef TETRA_CDEC_H
#define TETRA_CDEC_H

#include <stdint.h>

#if defined(_WIN32) && defined(TETRA_CDEC_BUILDING)
#define TETRA_CDEC_API __declspec(dllexport)
#elif defined(_WIN32)
#define TETRA_CDEC_API __declspec(dllimport)
#else
#define TETRA_CDEC_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Input: 690 little-endian int16 words (sync 0x6B21..0x6B26 + soft bits).
 * Output: 276 int16 words = 2 * (BFI + 137 speech bits), LSB = bit value.
 * Returns 0 on success, -1 on bad input.
 */
TETRA_CDEC_API int tetra_cdec_decode(const int16_t *block690, int16_t *serial276);

/* Reset internal first-pass / RCPC state (call at start of a stream). */
TETRA_CDEC_API void tetra_cdec_reset(void);

#ifdef __cplusplus
}
#endif

#endif
