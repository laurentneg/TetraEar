"""
In-process TETRA TCH codec: libtetra_cdec (channel) + mbelib-neo (ACELP speech).

Replaces subprocess cdecoder/sdecoder when the shared libraries are available.
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)
codec_logger = logging.getLogger("tetraear.codec")

_CODEC_ROOT = Path(__file__).resolve().parents[1] / "tetra_codec"
_LIB_DIR = _CODEC_ROOT / "lib"

MBE_TETRA_FRAME_BITS = 137
MBE_TETRA_FRAME_SAMPLES = 240
SERIAL_WORDS = 276  # 2 * (1 BFI + 137 bits)


class NativeCodecUnavailable(RuntimeError):
    pass


def _ensure_dll_search_path() -> None:
    """Windows: allow loading dependent DLLs from tetra_codec/lib."""
    if os.name != "nt":
        return
    if hasattr(os, "add_dll_directory") and _LIB_DIR.is_dir():
        try:
            os.add_dll_directory(str(_LIB_DIR))
        except OSError:
            pass
    # Fallback for older loaders / MinGW runtime DLLs beside the codec
    path = os.environ.get("PATH", "")
    lib = str(_LIB_DIR)
    if lib and lib not in path.split(os.pathsep):
        os.environ["PATH"] = lib + os.pathsep + path


def _candidate_lib_paths(name: str) -> list[Path]:
    """Search order: env override, tetra_codec/lib, system."""
    paths: list[Path] = []
    env = os.environ.get("TETRAEAR_CODEC_LIBDIR")
    if env:
        paths.append(Path(env) / name)
    paths.append(_LIB_DIR / name)
    # Versioned soname for mbelib (POSIX)
    if "mbe-neo" in name or name.startswith("libmbe"):
        paths.append(_LIB_DIR / "libmbe-neo.so.2")
        paths.append(_LIB_DIR / "libmbe-neo.so.2.6.2")
        paths.append(_LIB_DIR / "mbe-neo.dll")
        paths.append(_LIB_DIR / "libmbe-neo.dll")
    if os.name != "nt":
        paths.append(Path("/usr/local/lib") / name)
        paths.append(Path("/usr/lib") / name)
    return paths


def _load_cdll(names: list[str]) -> ctypes.CDLL:
    _ensure_dll_search_path()
    last_err: Optional[OSError] = None
    for name in names:
        for path in _candidate_lib_paths(name):
            if not path.exists():
                continue
            try:
                return ctypes.CDLL(str(path))
            except OSError as e:
                last_err = e
    raise NativeCodecUnavailable(
        f"Cannot load {names[0]} (tried under {_LIB_DIR}): {last_err}"
    )


def _cdec_lib_names() -> list[str]:
    if os.name == "nt":
        return ["tetra_cdec.dll", "libtetra_cdec.dll"]
    return ["libtetra_cdec.so"]


def _mbe_lib_names() -> list[str]:
    if os.name == "nt":
        return ["mbe-neo.dll", "libmbe-neo.dll"]
    return ["libmbe-neo.so", "libmbe-neo.so.2"]


class NativeTetraCodec:
    """Channel + speech decode for one TCH slot (690 int16 in → PCM out)."""

    def __init__(self) -> None:
        self._cdec = _load_cdll(_cdec_lib_names())
        self._mbe = _load_cdll(_mbe_lib_names())

        self._cdec.tetra_cdec_reset.argtypes = []
        self._cdec.tetra_cdec_reset.restype = None
        self._cdec.tetra_cdec_decode.argtypes = [
            ctypes.POINTER(ctypes.c_int16),
            ctypes.POINTER(ctypes.c_int16),
        ]
        self._cdec.tetra_cdec_decode.restype = ctypes.c_int

        self._mbe.mbe_tetraDecoderAlloc.argtypes = []
        self._mbe.mbe_tetraDecoderAlloc.restype = ctypes.c_void_p
        self._mbe.mbe_tetraDecoderFree.argtypes = [ctypes.c_void_p]
        self._mbe.mbe_tetraDecoderFree.restype = None
        self._mbe.mbe_tetraDecoderReset.argtypes = [ctypes.c_void_p]
        self._mbe.mbe_tetraDecoderReset.restype = None
        self._mbe.mbe_tetraDecodeFrame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16),
        ]
        self._mbe.mbe_tetraDecodeFrame.restype = ctypes.c_int

        self._dec = self._mbe.mbe_tetraDecoderAlloc()
        if not self._dec:
            raise NativeCodecUnavailable("mbe_tetraDecoderAlloc failed")
        self._cdec.tetra_cdec_reset()
        self.last_bfi = (1, 1)
        self.last_raw_peak = 0.0
        logger.info(
            "Native TETRA codec ready (libtetra_cdec + mbelib-neo) from %s",
            _LIB_DIR,
        )

    def close(self) -> None:
        if getattr(self, "_dec", None):
            self._mbe.mbe_tetraDecoderFree(self._dec)
            self._dec = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def reset(self) -> None:
        self._cdec.tetra_cdec_reset()
        if self._dec:
            self._mbe.mbe_tetraDecoderReset(self._dec)

    def decode_tch_block(self, frame_data: bytes) -> np.ndarray:
        """
        Decode one 1380-byte TCH block to float32 PCM @ 8 kHz.

        Returns empty array on hard failure. Bad frames still synthesize via
        mbelib-neo concealment when BFI is set.
        """
        if not frame_data or len(frame_data) != 1380:
            return np.zeros(0, dtype=np.float32)

        block = np.frombuffer(frame_data, dtype=np.int16).copy()
        if int(block[0]) & 0xFFFF != 0x6B21:
            codec_logger.debug("Native codec: bad header 0x%04x", int(block[0]) & 0xFFFF)
            return np.zeros(0, dtype=np.float32)

        serial = np.zeros(SERIAL_WORDS, dtype=np.int16)
        rc = self._cdec.tetra_cdec_decode(
            block.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            serial.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        )
        if rc != 0:
            codec_logger.debug("tetra_cdec_decode failed rc=%s", rc)
            return np.zeros(0, dtype=np.float32)

        bfi1 = int(serial[0])
        bfi2 = int(serial[138])
        self.last_bfi = (bfi1, bfi2)
        codec_logger.debug("Native cdec BFI: frame1=%s frame2=%s", bfi1, bfi2)

        pcm_parts: list[np.ndarray] = []
        for offset, bfi in ((1, bfi1), (139, bfi2)):
            bits = np.zeros(MBE_TETRA_FRAME_BITS, dtype=np.uint8)
            # Serial words: LSB of each int16 is the bit (ETSI format)
            for i in range(MBE_TETRA_FRAME_BITS):
                bits[i] = int(serial[offset + i]) & 1
            pcm = np.zeros(MBE_TETRA_FRAME_SAMPLES, dtype=np.int16)
            bad = 1 if bfi != 0 else 0
            bits_ptr = bits.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)) if bad == 0 else None
            st = self._mbe.mbe_tetraDecodeFrame(
                self._dec,
                bits_ptr,
                bad,
                pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            )
            if st != 0:
                codec_logger.debug("mbe_tetraDecodeFrame status=%s (bfi=%s)", st, bad)
            pcm_parts.append(pcm)

        audio_i16 = np.concatenate(pcm_parts)
        audio = audio_i16.astype(np.float32) / 32768.0
        self.last_raw_peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        return audio


_singleton: Optional[NativeTetraCodec] = None


def get_native_codec() -> Optional[NativeTetraCodec]:
    """Return a shared NativeTetraCodec, or None if libs are missing."""
    global _singleton
    if _singleton is not None:
        return _singleton
    try:
        _singleton = NativeTetraCodec()
        return _singleton
    except NativeCodecUnavailable as e:
        logger.info("Native codec unavailable: %s", e)
        return None
    except Exception as e:
        logger.warning("Native codec init failed: %s", e)
        return None
