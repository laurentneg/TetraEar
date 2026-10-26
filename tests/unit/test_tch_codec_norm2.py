"""TCH codec_input must be built for NORM_2 traffic (not only NORM_1)."""

import numpy as np

from tetraear.core.lower_mac import (
    BlockType,
    LowerMacBlock,
    LowerMacDecoder,
    NDB_BLK2_OFFSET,
    NDB_BLK_BITS,
    TrainSeq,
)


def test_attach_tch_codec_builds_1380_bytes():
    dec = LowerMacDecoder()
    dec.scramb_init = 0xABCDEF01
    blk = LowerMacBlock(BlockType.NDB, 1, None, False, np.zeros(216, dtype=np.uint8), dec.scramb_init)
    blk.is_traffic = True
    combined = np.random.default_rng(0).integers(0, 2, 432, dtype=np.uint8)
    dec._attach_tch_codec(blk, combined, dl_usage=5, verified=True)
    assert blk.codec_input is not None
    assert len(blk.codec_input) == 1380
    assert blk.extra["voice_verified"] is True


def test_norm2_traffic_attaches_codec_input(monkeypatch):
    need = NDB_BLK2_OFFSET + NDB_BLK_BITS
    bits = np.random.default_rng(1).integers(0, 2, need, dtype=np.uint8)

    orig = LowerMacDecoder._decode_block

    def fake_decode(self, block_type, b, *, force_scramb=None):
        if block_type == BlockType.BBK:
            # Access-Assign hdr=01, DL usage=5 (traffic)
            t1 = np.array([0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0], dtype=np.uint8)
            return LowerMacBlock(BlockType.BBK, 0, t1, True, b[:30], self.scramb_init)
        return orig(self, block_type, b, force_scramb=force_scramb)

    monkeypatch.setattr(LowerMacDecoder, "_decode_block", fake_decode)
    dec = LowerMacDecoder()
    dec.scramb_init = 0xABCDEF01
    blocks = dec.decode_burst(bits, TrainSeq.NORM_2)
    with_codec = [b for b in blocks if b.codec_input]
    assert len(with_codec) == 1
    assert len(with_codec[0].codec_input) == 1380
    assert with_codec[0].is_traffic is True
