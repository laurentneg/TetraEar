/************************************************************************
 * In-memory façade around ETSI Channel_Decoding (cdecoder without files).
 ************************************************************************/
#include "tetra_cdec.h"
#include "etsi_cdec/channel.h"

#include <string.h>

static short first_pass = 1;

void tetra_cdec_reset(void)
{
	first_pass = 1;
}

/* Unpack 690-word ETSI hardware test block → 432 soft bits (see Read_Tetra_File). */
static int unpack_tetra_block(const Word16 *block, Word16 *array432)
{
	Word16 *array = array432;
	const Word16 *ptr;
	int i;

	if ((block[0] & 0xFFFF) != 0x6B21)
		return -1;

	ptr = block + 1;
	for (i = 0; i < 114; i++)
		*array++ = *ptr++;

	ptr = block + (161 - 45);
	for (i = 0; i < 114; i++)
		*array++ = *ptr++;

	ptr = block + (321 - 45 - 45);
	for (i = 0; i < 114; i++)
		*array++ = *ptr++;

	ptr = block + (481 - 45 - 45 - 45);
	for (i = 0; i < 90; i++)
		*array++ = *ptr++;

	for (i = 0; i < 432; i++) {
		if ((array432[i] & 0x0080) == 0x0080)
			array432[i] = (Word16)(array432[i] | 0xFF00);
	}
	return 0;
}

int tetra_cdec_decode(const int16_t *block690, int16_t *serial276)
{
	Word16 Interleaved_coded_array[432];
	Word16 Coded_array[432];
	Word16 Reordered_array[286];
	Word16 bfi1;
	Word16 bfi2;
	Word16 Frame_stealing = 0;
	int i;

	if (!block690 || !serial276)
		return -1;

	if (unpack_tetra_block((const Word16 *)block690, Interleaved_coded_array) != 0)
		return -1;

	Desinterleaving_Speech(Interleaved_coded_array, Coded_array);
	bfi1 = Frame_stealing;

	bfi2 = Channel_Decoding(first_pass, Frame_stealing, Coded_array, Reordered_array);
	first_pass = 0;
	if ((Frame_stealing == 0) && (bfi2 == 1))
		bfi1 = 1;

	serial276[0] = bfi1;
	for (i = 0; i < 137; i++)
		serial276[1 + i] = Reordered_array[i];
	serial276[138] = bfi2;
	for (i = 0; i < 137; i++)
		serial276[139 + i] = Reordered_array[137 + i];

	return 0;
}
