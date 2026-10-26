"""
TETRA Voice Processor

Prefers in-process native libs (libtetra_cdec + mbelib-neo). Falls back to
ETSI TS 300 395-2 reference executables (cdecoder/sdecoder) when .so are missing.

Pipeline (per time-slot block):
1) Channel decode: soft bits → serial vocoder bits (2 × (BFI + 137))
2) Speech decode: serial → PCM @ 8 kHz (480 samples / slot)
"""

from __future__ import annotations

import os
import logging
import subprocess
import tempfile
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)
codec_logger = logging.getLogger("tetraear.codec")


class VoiceProcessor:
    """
    Handles TETRA voice decoding (native shared libs, else ETSI executables).
    """

    def __init__(
        self,
        codec_path: str | os.PathLike[str] | None = None,
        cdecoder_path: str | os.PathLike[str] | None = None,
        sdecoder_path: str | os.PathLike[str] | None = None,
        codec_dir: str | os.PathLike[str] | None = None,
        prefer_native: bool = True,
    ):
        """
        Create a voice decoder.

        Args:
            codec_path: Back-compat alias for `cdecoder_path` (path to `cdecoder`).
            cdecoder_path: Optional path to channel decoder executable.
            sdecoder_path: Optional path to speech decoder executable.
            codec_dir: Optional directory containing codec executables.
            prefer_native: If True (default), use libtetra_cdec + mbelib-neo when loadable.
        """
        default_dir = Path(__file__).resolve().parents[1] / "tetra_codec" / "bin"

        resolved_codec_dir = None
        if codec_dir is not None:
            resolved_codec_dir = Path(codec_dir)
        elif cdecoder_path is not None or codec_path is not None:
            resolved_codec_dir = Path(cdecoder_path or codec_path).resolve().parent
        else:
            resolved_codec_dir = default_dir

        def resolve_candidate(provided: str | os.PathLike[str] | None, candidates: list[str]) -> Path:
            if provided is not None:
                return Path(provided)
            for name in candidates:
                candidate = resolved_codec_dir / name
                if candidate.exists():
                    return candidate
            return resolved_codec_dir / candidates[0]

        self.cdecoder_path = resolve_candidate(cdecoder_path or codec_path, ["cdecoder", "cdecoder.exe"])
        self.sdecoder_path = resolve_candidate(sdecoder_path, ["sdecoder", "sdecoder.exe"])

        for path in (self.cdecoder_path, self.sdecoder_path):
            if path.exists() and os.name != "nt":
                try:
                    if not os.access(path, os.X_OK):
                        path.chmod(path.stat().st_mode | 0o111)
                except Exception:
                    pass

        def is_windows_binary(path: Path) -> bool:
            try:
                with path.open("rb") as handle:
                    return handle.read(2) == b"MZ"
            except Exception:
                return False

        self._native = None
        if prefer_native:
            try:
                from tetraear.audio.native_codec import get_native_codec

                self._native = get_native_codec()
            except Exception as e:
                logger.debug("Native codec probe failed: %s", e)
                self._native = None

        self.channel_decoder_available = self.cdecoder_path.exists() and os.access(
            self.cdecoder_path, os.X_OK
        )
        self.speech_decoder_available = self.sdecoder_path.exists() and os.access(
            self.sdecoder_path, os.X_OK
        )
        self.exe_working = self.channel_decoder_available and self.speech_decoder_available
        self.native = self._native is not None
        self.working = self.native or self.exe_working
        self.last_raw_peak = 0.0
        # Packet-loss concealment: last good 8 kHz frame (ETSI ACELP is ~480 samples/slot)
        self._last_good_audio: np.ndarray | None = None
        self._bfi_streak: int = 0

        if self.native:
            logger.info("TETRA voice path: native (libtetra_cdec + mbelib-neo)")
        else:
            if not self.channel_decoder_available:
                logger.warning("TETRA codec channel decoder not found at %s", self.cdecoder_path)
            else:
                if os.name != "nt" and is_windows_binary(self.cdecoder_path):
                    logger.warning(
                        "Windows codec detected at %s; install native codec or use Wine",
                        self.cdecoder_path,
                    )
                logger.debug("TETRA codec channel decoder found at %s", self.cdecoder_path)

            if not self.speech_decoder_available:
                logger.warning("TETRA codec speech decoder not found at %s", self.sdecoder_path)
            else:
                if os.name != "nt" and is_windows_binary(self.sdecoder_path):
                    logger.warning(
                        "Windows codec detected at %s; install native codec or use Wine",
                        self.sdecoder_path,
                    )
                logger.debug("TETRA codec speech decoder found at %s", self.sdecoder_path)
            if self.exe_working:
                logger.info("TETRA voice path: subprocess executables (cdecoder/sdecoder)")

    def decode_frame(self, frame_data: bytes) -> np.ndarray:
        """
        Decode a TETRA time-slot block into synthesized audio.

        Input (`frame_data`) must be a binary file image containing 690 16-bit words:
        - First word: 0x6B21 (header marker)
        - Next 689 words: soft bits (16-bit values; typical range -127..127)

        Returns:
            Float32 numpy array of audio samples in [-1, 1]. Empty array on failure.
        """
        if not self.working or not frame_data:
            return np.zeros(0)

        if self._native is not None:
            return self._decode_native(frame_data)
        return self._decode_exe(frame_data)

    def _postprocess(
        self,
        audio: np.ndarray,
        *,
        bfi1: int,
        bfi2: int,
        raw_peak: float,
    ) -> np.ndarray:
        both_bfi = bfi1 != 0 and bfi2 != 0
        if both_bfi:
            self._bfi_streak += 1
        else:
            self._bfi_streak = 0

        # Near-silent or missing synth → PLC from last good frame
        if audio.size == 0 or raw_peak < 1e-5:
            return self._plc_audio(reason="silent_or_missing")

        # Reject white-ish garbage (wrong bits), then PLC if we have history
        if audio.size >= 160:
            windowed = audio * np.hanning(audio.size)
            spec = np.abs(np.fft.rfft(windowed)) + 1e-12
            flatness = float(np.exp(np.mean(np.log(spec))) / np.mean(spec))
            if flatness > 0.88:
                codec_logger.debug(
                    "Rejecting noise-like codec audio (flatness=%.3f raw_peak=%.5f)",
                    flatness,
                    raw_peak,
                )
                return self._plc_audio(reason=f"flatness={flatness:.3f}")

        # After a long BFI run, mute rather than stretch one syllable forever
        if both_bfi and self._bfi_streak > 8:
            codec_logger.debug("BFI streak=%s — muting (end of usable concealment)", self._bfi_streak)
            self.last_raw_peak = 0.0
            return np.zeros(0, dtype=np.float32)

        # Soften concealed frames so errors don't blast
        if both_bfi and audio.size > 0:
            audio = audio * 0.65

        # Normalize quiet frames to a clearly audible peak.
        max_amp = float(np.max(np.abs(audio))) if audio.size else 0.0
        if 0 < max_amp < 0.35:
            gain = min(0.55 / max_amp, 800.0)
            audio = np.clip(audio * gain, -1.0, 1.0)

        if not both_bfi and audio.size > 0:
            self._last_good_audio = audio.copy()

        self.last_raw_peak = raw_peak
        if audio.size > 0:
            logger.debug(
                "Decoded %d audio samples from voice frame (raw_peak=%.5f bfi=%s/%s native=%s)",
                audio.size,
                raw_peak,
                bfi1,
                bfi2,
                self.native,
            )
        return audio

    def _decode_native(self, frame_data: bytes) -> np.ndarray:
        try:
            if len(frame_data) != 1380:
                logger.debug("Invalid frame size: %s bytes (expected 1380)", len(frame_data))
                return np.zeros(0)

            audio = self._native.decode_tch_block(frame_data)
            bfi1, bfi2 = self._native.last_bfi
            raw_peak = float(self._native.last_raw_peak)
            return self._postprocess(audio, bfi1=bfi1, bfi2=bfi2, raw_peak=raw_peak)
        except Exception as e:
            logger.debug("Native voice decode error: %s", e)
            import traceback

            logger.debug(traceback.format_exc())
            self.last_raw_peak = 0.0
            return self._plc_audio(reason="exception")

    def _decode_exe(self, frame_data: bytes) -> np.ndarray:
        try:
            import struct

            if len(frame_data) != 1380:
                logger.debug("Invalid frame size: %s bytes (expected 1380)", len(frame_data))
                return np.zeros(0)

            header = struct.unpack("<H", frame_data[0:2])[0]
            if header != 0x6B21:
                logger.debug("Invalid header: 0x%04X (expected 0x6B21)", header)
                return np.zeros(0)

            keep_temp = os.environ.get("TETRAEAR_KEEP_CODEC_TEMP", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "y",
            )

            with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".tet") as tmp_in:
                tmp_in.write(frame_data)
                tmp_in_path = tmp_in.name

            tmp_serial_path = tmp_in_path + ".serial"
            tmp_synth_path = tmp_in_path + ".synth"

            input_shorts = np.frombuffer(frame_data, dtype=np.int16)
            logger.debug(
                "Codec input: %s shorts, Max: %s, Header: 0x%04X",
                len(input_shorts),
                np.max(np.abs(input_shorts)),
                int(input_shorts[0]) & 0xFFFF,
            )

            abs_in = os.path.abspath(tmp_in_path)
            abs_serial = os.path.abspath(tmp_serial_path)
            abs_synth = os.path.abspath(tmp_synth_path)

            codec_logger.debug("Calling cdecoder: %s %s %s", self.cdecoder_path, abs_in, abs_serial)
            result = subprocess.run(
                [str(self.cdecoder_path), abs_in, abs_serial],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
            )

            if result.stdout:
                codec_logger.debug(
                    "STDOUT: %s", result.stdout.decode("utf-8", errors="ignore").strip()
                )
            if result.stderr:
                codec_logger.debug(
                    "STDERR: %s", result.stderr.decode("utf-8", errors="ignore").strip()
                )

            if result.returncode != 0:
                codec_logger.debug("cdecoder failed with return code %s", result.returncode)
            else:
                codec_logger.debug("cdecoder exited 0")

            if not (os.path.exists(tmp_serial_path) and os.path.getsize(tmp_serial_path) > 0):
                codec_logger.debug(
                    "cdecoder produced no serial output file or empty file (return code: %s)",
                    result.returncode,
                )
                if not keep_temp:
                    try:
                        os.remove(tmp_in_path)
                        if os.path.exists(tmp_serial_path):
                            os.remove(tmp_serial_path)
                        if os.path.exists(tmp_synth_path):
                            os.remove(tmp_synth_path)
                    except Exception:
                        pass
                return np.zeros(0)

            serial_size = os.path.getsize(tmp_serial_path)
            codec_logger.debug("cdecoder serial output: %s (%s bytes)", tmp_serial_path, serial_size)

            bfi1 = 1
            bfi2 = 1
            try:
                with open(tmp_serial_path, "rb") as f:
                    raw = f.read(min(serial_size, 552))
                if len(raw) >= 2:
                    bfi1 = struct.unpack("<h", raw[:2])[0]
                    if len(raw) >= 276 + 2:
                        bfi2 = struct.unpack("<h", raw[276:278])[0]
                    codec_logger.debug("cdecoder BFI: frame1=%s frame2=%s", bfi1, bfi2)
            except Exception:
                pass

            codec_logger.debug("Calling sdecoder: %s %s %s", self.sdecoder_path, abs_serial, abs_synth)
            result2 = subprocess.run(
                [str(self.sdecoder_path), abs_serial, abs_synth],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
            )

            if result2.stdout:
                codec_logger.debug(
                    "SDECODER STDOUT: %s", result2.stdout.decode("utf-8", errors="ignore").strip()
                )
            if result2.stderr:
                codec_logger.debug(
                    "SDECODER STDERR: %s", result2.stderr.decode("utf-8", errors="ignore").strip()
                )

            if result2.returncode != 0:
                codec_logger.debug("sdecoder failed with return code %s", result2.returncode)

            audio = np.zeros(0, dtype=np.float32)
            raw_peak = 0.0
            if os.path.exists(tmp_synth_path) and os.path.getsize(tmp_synth_path) > 0:
                with open(tmp_synth_path, "rb") as f:
                    pcm_data = f.read()
                audio_i16 = np.frombuffer(pcm_data, dtype=np.int16)
                if audio_i16.size > 0:
                    audio = audio_i16.astype(np.float32) / 32768.0
                    raw_peak = float(np.max(np.abs(audio)))
                    codec_logger.debug("Codec produced %d samples (max amp %.4f)", audio.size, raw_peak)

            if not keep_temp:
                try:
                    os.remove(tmp_in_path)
                    if os.path.exists(tmp_serial_path):
                        os.remove(tmp_serial_path)
                    if os.path.exists(tmp_synth_path):
                        os.remove(tmp_synth_path)
                except Exception:
                    pass

            return self._postprocess(audio, bfi1=bfi1, bfi2=bfi2, raw_peak=raw_peak)

        except Exception as e:
            logger.debug("Voice decode error: %s", e)
            import traceback

            logger.debug(traceback.format_exc())
            self.last_raw_peak = 0.0
            return self._plc_audio(reason="exception")

    def _plc_audio(self, *, reason: str) -> np.ndarray:
        """Repeat last good frame (attenuated) instead of a hard silence gap."""
        self.last_raw_peak = 0.0
        if self._last_good_audio is None or self._last_good_audio.size == 0:
            codec_logger.debug("PLC empty (%s) — no prior good frame", reason)
            return np.zeros(0, dtype=np.float32)
        if self._bfi_streak > 8:
            codec_logger.debug("PLC muted (%s) after long BFI streak", reason)
            return np.zeros(0, dtype=np.float32)
        fade = max(0.25, 0.85 ** max(1, self._bfi_streak))
        out = (self._last_good_audio * fade).astype(np.float32)
        codec_logger.debug("PLC reuse (%s) fade=%.2f samples=%s", reason, fade, out.size)
        self.last_raw_peak = float(np.max(np.abs(out))) if out.size else 0.0
        return out
