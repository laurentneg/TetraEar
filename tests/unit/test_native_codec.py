"""Unit tests for in-process TETRA codec (libtetra_cdec + mbelib-neo)."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / "tetraear" / "tetra_codec" / "lib"
RECORDS = ROOT / "records"


def _libs_present() -> bool:
    if not (LIB_DIR / "libtetra_cdec.so").exists():
        return False
    return (LIB_DIR / "libmbe-neo.so").exists() or (LIB_DIR / "libmbe-neo.so.2").exists()


pytestmark = pytest.mark.skipif(not _libs_present(), reason="native codec .so not built")


def _synthetic_block() -> bytes:
    frame = bytearray()
    frame.extend(struct.pack("<H", 0x6B21))
    for i in range(689):
        frame.extend(struct.pack("<h", (i % 2) * 64))
    return bytes(frame)


def _capture_block() -> bytes | None:
    bins = sorted(RECORDS.glob("tetra_frames_*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in bins:
        data = path.read_bytes()
        if len(data) >= 1380:
            return data[:1380]
    return None


def test_native_decode_returns_480_with_bfi_concealment():
    from tetraear.audio.native_codec import NativeTetraCodec

    frame = _capture_block() or _synthetic_block()
    codec = NativeTetraCodec()
    try:
        audio = codec.decode_tch_block(frame)
        assert audio.dtype == np.float32
        assert audio.size == 480
        bfi1, bfi2 = codec.last_bfi
        assert bfi1 in (0, 1)
        assert bfi2 in (0, 1)
        # Concealment / silence still yields a finite buffer
        assert np.all(np.isfinite(audio))
    finally:
        codec.close()


def test_native_matches_cdecoder_serial_when_exe_present():
    """Channel stage must match the ETSI cdecoder executable bit-for-bit."""
    import ctypes
    import subprocess
    import tempfile

    cdec_exe = ROOT / "tetraear" / "tetra_codec" / "bin" / "cdecoder"
    if not cdec_exe.exists():
        pytest.skip("cdecoder executable not installed")

    frame = _capture_block() or _synthetic_block()
    lib = ctypes.CDLL(str(LIB_DIR / "libtetra_cdec.so"))
    lib.tetra_cdec_reset()
    lib.tetra_cdec_decode.argtypes = [
        ctypes.POINTER(ctypes.c_int16),
        ctypes.POINTER(ctypes.c_int16),
    ]
    lib.tetra_cdec_decode.restype = ctypes.c_int

    block = np.frombuffer(frame, dtype=np.int16).copy()
    serial = np.zeros(276, dtype=np.int16)
    rc = lib.tetra_cdec_decode(
        block.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        serial.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
    )
    assert rc == 0

    with tempfile.NamedTemporaryFile(suffix=".tet", delete=False) as f:
        f.write(frame)
        inp = f.name
    out = inp + ".serial"
    try:
        subprocess.run([str(cdec_exe), inp, out], check=True, capture_output=True)
        exe = np.fromfile(out, dtype=np.int16)
        assert exe.size >= 276
        assert np.array_equal(serial, exe[:276])
    finally:
        Path(inp).unlink(missing_ok=True)
        Path(out).unlink(missing_ok=True)


def test_voice_processor_prefers_native():
    from tetraear.audio.voice import VoiceProcessor

    vp = VoiceProcessor(prefer_native=True)
    assert vp.working
    assert vp.native

    frame = _capture_block() or _synthetic_block()
    audio = vp.decode_frame(frame)
    # May be empty after PLC mute on all-BFI synthetic noise; size is 0 or 480
    assert audio.size in (0, 480)
    if audio.size == 480:
        assert audio.dtype == np.float32
