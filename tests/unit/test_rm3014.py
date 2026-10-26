"""Unit tests for TETRA AACH RM(30,14) coding."""

import numpy as np
import pytest

from tetraear.core.lower_mac import rm3014_decode, rm3014_encode


def _bits_from_codeword(code: int) -> np.ndarray:
    bits = np.zeros(30, dtype=np.uint8)
    for i in range(30):
        bits[i] = (code >> (29 - i)) & 1
    return bits


def test_rm3014_roundtrip_zero():
    code = rm3014_encode(0)
    decoded = rm3014_decode(_bits_from_codeword(code))
    assert decoded is not None
    assert int("".join(str(int(b)) for b in decoded), 2) == 0


def test_rm3014_roundtrip_sample_values():
    for data in (1, 0x1555, 0x2AAA, 0x3FFF, 0x1234):
        code = rm3014_encode(data)
        decoded = rm3014_decode(_bits_from_codeword(code))
        assert decoded is not None
        value = 0
        for b in decoded:
            value = (value << 1) | int(b)
        assert value == data


def test_rm3014_corrects_two_bit_errors():
    data = 0x0A5C
    code = rm3014_encode(data)
    bits = _bits_from_codeword(code)
    bits[3] ^= 1
    bits[17] ^= 1
    decoded = rm3014_decode(bits)
    assert decoded is not None
    value = 0
    for b in decoded:
        value = (value << 1) | int(b)
    assert value == data


def test_rm3014_soft_bits():
    data = 0x00FF
    code = rm3014_encode(data)
    hard = _bits_from_codeword(code)
    soft = np.where(hard == 1, -100, 100).astype(np.int16)
    decoded = rm3014_decode(soft)
    assert decoded is not None
    value = 0
    for b in decoded:
        value = (value << 1) | int(b)
    assert value == data


def test_rm3014_rejects_random_noise():
    rng = np.random.default_rng(42)
    accepted = 0
    for _ in range(100):
        bits = rng.integers(0, 2, 30, dtype=np.uint8)
        if rm3014_decode(bits) is not None:
            accepted += 1
    # With max_dist=2, random 30-bit words should almost never decode
    assert accepted <= 5
