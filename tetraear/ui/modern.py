"""
Modern TETRA Decoder GUI with Waterfall Spectrum.

This module lives under `tetraear.ui` and is the primary application UI.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
import sys
import threading
import queue
import logging
import os
import colorama
from colorama import Fore, Back, Style

def _get_runtime_root() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[2]


def _get_user_data_dir() -> Path:
    override = os.environ.get("TETRAEAR_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "TetraEar"
    return Path.home() / ".tetraear"


RUNTIME_ROOT = _get_runtime_root()
USER_DATA_DIR = _get_user_data_dir()

# Keep logs/records configurable and predictable for both source runs and bundled builds.
def _get_records_dir() -> Path:
    override = os.environ.get("TETRAEAR_RECORDS_DIR")
    if override:
        return Path(override)
    if getattr(sys, "_MEIPASS", None):
        return USER_DATA_DIR / "records"
    return RUNTIME_ROOT / "records"

# Assets live under the package (`tetraear/assets`) in source and in PyInstaller.
_pkg_root = Path(__file__).resolve().parents[1]
_bundled_assets = RUNTIME_ROOT / "tetraear" / "assets"
ASSETS_DIR = _bundled_assets if _bundled_assets.exists() else (_pkg_root / "assets")
RECORDS_DIR = _get_records_dir()
RECORDS_DIR.mkdir(parents=True, exist_ok=True)

# Add DLL path for RTL-SDR libraries
for dll_path in (RUNTIME_ROOT / "tetraear" / "bin", RUNTIME_ROOT / "dist", RUNTIME_ROOT):
    if dll_path.exists():
        os.environ["PATH"] = str(dll_path) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(dll_path))
            except OSError:
                pass

# Initialize colorama for Windows support
colorama.init(autoreset=True)

class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for console output."""
    
    COLORS = {
        'DEBUG': Fore.CYAN,
        'INFO': Fore.GREEN,
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'CRITICAL': Fore.RED + Back.WHITE + Style.BRIGHT
    }
    
    def format(self, record):
        # Add color to level name for console
        if hasattr(sys.stdout, 'isatty') and sys.stdout.isatty():
            levelname = record.levelname
            if levelname in self.COLORS:
                record.levelname = f"{self.COLORS[levelname]}{levelname}{Style.RESET_ALL}"
        return super().format(record)


class _PrefixFilter(logging.Filter):
    def __init__(self, *prefixes: str):
        super().__init__()
        self._prefixes = tuple(p for p in prefixes if p)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefixes) if self._prefixes else True


_RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")


def _get_log_dir() -> Path:
    override = os.environ.get("TETRAEAR_LOG_DIR")
    if override:
        return Path(override)
    # In PyInstaller builds, write logs to user data. In source runs, keep logs in repo-local `logs/`.
    if getattr(sys, "_MEIPASS", None):
        return USER_DATA_DIR / "logs"
    return RUNTIME_ROOT / "logs"


# Per-frame JSONL dumps are expensive (tens of MB/session). Off unless --debug.
_DEBUG_FRAME_LOG = False


def _setup_logging(debug: bool) -> dict[str, Path]:
    """
    Configure structured logging for the application.

    Default (no --debug): INFO only, no per-frame JSONL file.
    With --debug / -v: DEBUG files + `frames_<run>.log` (one JSON line per frame).

    Creates log files under the logs directory:
      - `tetraear_<run>.log`   (everything)
      - `app_<run>.log`       (UI + capture/signal pipeline)
      - `decoder_<run>.log`   (decoder/protocol)
      - `codec_<run>.log`     (codec calls + stdout/stderr)
      - `audio_<run>.log`     (recording + audio pipeline)
      - `frames_<run>.log`    (frames as JSONL — only if debug)
    """
    global _DEBUG_FRAME_LOG
    _DEBUG_FRAME_LOG = bool(debug)

    log_dir = _get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "all": log_dir / f"tetraear_{_RUN_ID}.log",
        "app": log_dir / f"app_{_RUN_ID}.log",
        "decoder": log_dir / f"decoder_{_RUN_ID}.log",
        "codec": log_dir / f"codec_{_RUN_ID}.log",
        "audio": log_dir / f"audio_{_RUN_ID}.log",
        "frames": log_dir / f"frames_{_RUN_ID}.log",
    }

    fmt = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_level = logging.DEBUG if debug else logging.INFO

    def make_file_handler(path: Path, *, level: int = logging.DEBUG, filt: logging.Filter | None = None) -> logging.Handler:
        handler = logging.FileHandler(str(path), encoding="utf-8", delay=True)
        handler.setLevel(level)
        handler.setFormatter(fmt)
        if filt is not None:
            handler.addFilter(filt)
        return handler

    all_handler = make_file_handler(files["all"], level=file_level)
    app_handler = make_file_handler(files["app"], level=file_level, filt=_PrefixFilter("tetraear.ui.modern", "tetraear.signal"))
    decoder_handler = make_file_handler(files["decoder"], level=file_level, filt=_PrefixFilter("tetraear.core.decoder", "tetraear.core.protocol"))
    codec_handler = make_file_handler(files["codec"], level=file_level, filt=_PrefixFilter("tetraear.codec"))
    audio_handler = make_file_handler(files["audio"], level=file_level, filt=_PrefixFilter("tetraear.recording", "tetraear.audio"))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    console_handler.setFormatter(ColoredFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()
    root.addHandler(all_handler)
    root.addHandler(app_handler)
    root.addHandler(decoder_handler)
    root.addHandler(codec_handler)
    root.addHandler(audio_handler)
    root.addHandler(console_handler)

    frames_logger_obj = logging.getLogger("tetraear.frames")
    if debug:
        frames_handler = make_file_handler(files["frames"], level=logging.INFO, filt=_PrefixFilter("tetraear.frames"))
        root.addHandler(frames_handler)
        frames_logger_obj.setLevel(logging.INFO)
    else:
        # Keep logger quiet; avoid creating a huge frames_*.log by default
        frames_logger_obj.setLevel(logging.WARNING)
        frames_logger_obj.propagate = False

    # Route `warnings.warn(...)` through logging too.
    logging.captureWarnings(True)

    return files


logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("tetraear.recording")
frames_logger = logging.getLogger("tetraear.frames")

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QComboBox, QSpinBox,
    QGroupBox, QCheckBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QProgressBar, QSlider, QFileDialog, QMessageBox, QSplitter, QFrame,
    QScrollArea, QSizePolicy, QHeaderView, QDialog
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, pyqtSlot, QSize, QRect, QObject
from PyQt6.QtGui import QFont, QColor, QPalette, QPainter, QLinearGradient, QPen, QBrush, QPixmap, QImage, QPainterPath, QIcon

import queue
import threading
import numpy as np
import sounddevice as sd
from scipy.signal import resample
import subprocess
import tempfile

from tetraear.signal.capture import RTLCapture
from tetraear.signal.processor import SignalProcessor
from tetraear.core.decoder import TetraDecoder
from tetraear.core.crypto import TetraKeyManager
from tetraear.core.mcc_mnc import get_location_info
from tetraear.core.validator import TetraSignalValidator
from tetraear.core.location import LocationParser
from tetraear.signal.scanner import FrequencyScanner
from tetraear.audio.voice import VoiceProcessor


def _is_readable_text(text: str) -> bool:
    """Check if text is actually human-readable (not garbled GSM7/encrypted)."""
    if not text or len(text) < 3:
        return False
    
    # Remove common prefixes and quotes
    clean = text
    for prefix in ['[GSM7]', '[TXT]', '[SDS]', '[SDS-1]', '[SDS-GSM]', '[LIP]', '[LOC]', '[GPS]', '[BIN-ENC]', '[BIN]', '💬', '🧩', '"']:
        clean = clean.replace(prefix, '')
    clean = clean.strip()
    
    if len(clean) < 3:
        return False
    
    # Check for GSM7 special characters that indicate garbled text (expanded)
    gsm7_specials = set('ΩΔΣΘΞΛΓΦΨΠåæÅÆØøÇÉÑÜßìÌíÍîÎïÏòÒóÓôÔõÕöÖùÙúÚûÛüÜ¿¡¢£¤¥¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾')
    special_count = sum(1 for c in clean if c in gsm7_specials)
    
    # If ANY special characters, it's likely garbled (very strict)
    if special_count > 0:
        return False
    
    # Check for high-byte characters that aren't in normal text
    high_byte_count = sum(1 for c in clean if ord(c) > 127)
    if len(clean) > 0 and high_byte_count / len(clean) > 0.05:
        return False
    
    # Check for non-printable ASCII characters
    control_chars = sum(1 for c in clean if ord(c) < 32 and c not in '\n\r\t')
    if control_chars > 0:
        return False
    
    # Check for ASCII alphanumeric content or spaces
    ascii_readable = sum(1 for c in clean if (c.isalnum() or c.isspace()) and ord(c) < 128)
    
    # Need at least 70% ASCII readable characters (very strict)
    if len(clean) > 0 and ascii_readable / len(clean) < 0.70:
        return False
    
    # Check for minimum alphanumeric content
    alnum_count = sum(1 for c in clean if c.isalnum())
    if len(clean) > 0 and alnum_count / len(clean) < 0.50:
        return False
    
    # Check for reasonable word structure - should have some spaces or clear word boundaries
    # If it's random gibberish, it won't have natural word patterns
    if len(clean) > 10:
        # Count lowercase letters (natural text has many)
        lowercase_count = sum(1 for c in clean if c.islower())
        if lowercase_count / len(clean) < 0.20:  # Less than 20% lowercase = suspicious
            return False
        
        # Check for word-like patterns (letters with spaces)
        words = clean.split()
        if len(words) > 1:
            # Check if "words" look reasonable (mostly letters)
            valid_words = sum(1 for w in words if len(w) > 0 and sum(1 for c in w if c.isalpha()) / len(w) > 0.5)
            if valid_words / len(words) < 0.5:
                return False
    elif len(clean) <= 10:
        # For short strings, be extra strict
        # Must have at least one lowercase letter
        if not any(c.islower() for c in clean):
            # Exception for short status codes like "OK", "ACK", etc.
            if not (len(clean) <= 4 and clean.isupper() and clean.isalpha()):
                return False
    
    return True


def _format_location_data(frame: dict) -> str:
    """Format location data nicely."""
    text = frame.get('decoded_text', '') or frame.get('sds_message', '')
    
    if '[LIP]' in text or '[LOC]' in text:
        # Try to parse latitude/longitude
        if 'Lat:' in text and 'Lon:' in text:
            return f"📍 {text}"
        else:
            # Just hex location data
            hex_data = text.split(':', 1)[-1].strip() if ':' in text else text
            return f"📍 Location Data: {hex_data[:40]}..."
    
    if '[GPS]' in text:
        return f"🛰️ {text}"
    
    return None


def _format_binary_metadata(frame: dict) -> str:
    """Format binary metadata and control frames."""
    if '[BIN-ENC]' in str(frame.get('decoded_text', '')):
        # This is encrypted binary data
        text = frame.get('decoded_text', '')
        if 'bytes' in text:
            # Extract byte count
            return f"🔐 Encrypted Binary Data ({text})"
        return "🔐 Encrypted Binary Data"
    
    # Check for control frame types
    type_name = frame.get('type_name', '')
    if type_name == 'MAC-RESOURCE':
        info = frame.get('additional_info', {})
        if info.get('talkgroup'):
            return f"📡 Resource Allocation: TG {info['talkgroup']}"
        return "📡 Resource Allocation"
    
    if type_name == 'MAC-BROADCAST':
        info = frame.get('additional_info', {})
        desc = info.get('description', '')
        if 'Broadcast' in desc or 'info' in desc.lower():
            return f"📢 Network Broadcast: {desc}"
        return "📢 Network Broadcast"
    
    if type_name in ['MAC-FRAG', 'MAC-END/RES']:
        return f"🔗 {type_name} (Fragment/Control)"
    
    return None


import json

class SettingsManager:
    """Manage application settings."""
    
    DEFAULT_SETTINGS = {
        "save_silence": False,
        "export_mp3": False,
        "auto_decrypt": True,
        "monitor_audio": True,
        "monitor_raw": False,
        "gain": 50.0,
        "sample_rate": 2.4e6,
        "last_frequency": 390.865,
        "bandwidth": 25000,
        "zoom_level": 1.0,
        "noise_floor": -85,
        "theme": "dark",
        "expected_mcc": None,
        "sdr_device": None,
    }
    
    def __init__(self, filename="settings.json"):
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.filename = str(USER_DATA_DIR / filename)
        self.settings = self.DEFAULT_SETTINGS.copy()
        self.load()
        
    def load(self):
        """Load settings from file."""
        try:
            if os.path.exists(self.filename):
                with open(self.filename, 'r') as f:
                    loaded = json.load(f)
                    self.settings.update(loaded)
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            
    def save(self):
        """Save settings to file."""
        try:
            with open(self.filename, 'w') as f:
                json.dump(self.settings, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            
    def get(self, key, default=None):
        return self.settings.get(key, default)
        
    def set(self, key, value, *, persist: bool = False):
        self.settings[key] = value
        if persist:
            self.save()

class FrequencyManager:
    """Manage saved frequencies."""
    
    def __init__(self, filename="frequencies.json"):
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.filename = str(USER_DATA_DIR / filename)
        self.frequencies = []
        self.load()
        
    def load(self):
        """Load frequencies from file."""
        try:
            if os.path.exists(self.filename):
                with open(self.filename, 'r') as f:
                    self.frequencies = json.load(f)
            else:
                # Defaults
                self.frequencies = [
                    {"freq": 390.000, "label": "TETRA PL 1", "desc": "Poland Public Safety"},
                    {"freq": 392.500, "label": "TETRA PL 2", "desc": "Poland Public Safety"},
                    {"freq": 420.000, "label": "TETRA EU", "desc": "Europe General"},
                ]
        except Exception as e:
            logger.error(f"Failed to load frequencies: {e}")
            
    def save(self):
        """Save frequencies to file."""
        try:
            with open(self.filename, 'w') as f:
                json.dump(self.frequencies, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save frequencies: {e}")
            
    def add(self, freq, label, desc=""):
        self.frequencies.append({"freq": freq, "label": label, "desc": desc})
        self.save()
        
    def get_all(self):
        return self.frequencies

class SettingsDialog(QDialog):
    """Settings configuration dialog."""
    
    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        self.setWindowTitle("Settings")
        self.setMinimumWidth(400)
        self.init_ui()
        self.apply_style()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Recording Settings
        rec_group = QGroupBox("Recording")
        rec_layout = QVBoxLayout()
        
        self.save_silence_cb = QCheckBox("Save Silent Audio Files")
        self.save_silence_cb.setChecked(self.settings.get("save_silence", False))
        self.save_silence_cb.setToolTip("If unchecked, .wav files with no voice activity will be deleted.")
        rec_layout.addWidget(self.save_silence_cb)

        self.export_mp3_cb = QCheckBox("Export MP3 (requires ffmpeg)")
        self.export_mp3_cb.setChecked(self.settings.get("export_mp3", False))
        self.export_mp3_cb.setToolTip("If enabled, finished voice recordings are converted to .mp3 using ffmpeg if available.")
        rec_layout.addWidget(self.export_mp3_cb)
        
        rec_group.setLayout(rec_layout)
        layout.addWidget(rec_group)

        # SDR Settings
        sdr_group = QGroupBox("SDR")
        sdr_layout = QVBoxLayout()
        sdr_layout.addWidget(QLabel("Device:"))
        self.sdr_device_combo = QComboBox()
        self.sdr_device_combo.addItem("Default (first available)", None)
        try:
            from tetraear.signal.capture import RTLCapture
            for dev in RTLCapture.list_devices():
                serial = dev.get("serial") or ""
                idx = int(dev.get("index", 0))
                label = f"#{idx} — {serial}" if serial else f"#{idx}"
                # Persist by index: stock dongles often share the same serial
                self.sdr_device_combo.addItem(label, idx)
            current_sdr = self.settings.get("sdr_device", None)
            if current_sdr is not None:
                # Match index, or fall back to first device with that serial
                index = self.sdr_device_combo.findData(current_sdr)
                if index < 0 and isinstance(current_sdr, str):
                    for i in range(self.sdr_device_combo.count()):
                        text = self.sdr_device_combo.itemText(i)
                        if current_sdr in text:
                            index = i
                            break
                if index >= 0:
                    self.sdr_device_combo.setCurrentIndex(index)
        except Exception as e:
            logger.error(f"Failed to query RTL-SDR devices: {e}")
        sdr_layout.addWidget(self.sdr_device_combo)
        sdr_group.setLayout(sdr_layout)
        layout.addWidget(sdr_group)
        
        # Audio Settings
        audio_group = QGroupBox("Audio")
        audio_layout = QVBoxLayout()
        
        # Audio Device Selection
        audio_layout.addWidget(QLabel("Output Device:"))
        self.audio_device_combo = QComboBox()
        self.audio_device_combo.addItem("Default", None)
        
        # Populate audio devices
        try:
            devices = sd.query_devices()
            default_output = sd.default.device[1]
            for i, dev in enumerate(devices):
                if dev['max_output_channels'] > 0:
                    name = dev['name']
                    if i == default_output:
                        name += " (Default)"
                    self.audio_device_combo.addItem(name, i)
                    
            # Select current device
            current_device = self.settings.get("audio_device", None)
            if current_device is not None:
                index = self.audio_device_combo.findData(current_device)
                if index >= 0:
                    self.audio_device_combo.setCurrentIndex(index)
        except Exception as e:
            logger.error(f"Failed to query audio devices: {e}")
            
        audio_layout.addWidget(self.audio_device_combo)
        audio_group.setLayout(audio_layout)
        layout.addWidget(audio_group)
        
        # Appearance Settings
        app_group = QGroupBox("Appearance")
        app_layout = QVBoxLayout()
        
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark (Default)", "Light", "High Contrast"])
        current_theme = self.settings.get("theme", "Dark (Default)")
        self.theme_combo.setCurrentText(current_theme)
        
        app_layout.addWidget(QLabel("Theme:"))
        app_layout.addWidget(self.theme_combo)
        app_group.setLayout(app_layout)
        layout.addWidget(app_group)
        
        # Buttons
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_settings)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)
        
    def save_settings(self):
        self.settings.set("save_silence", self.save_silence_cb.isChecked())
        self.settings.set("export_mp3", self.export_mp3_cb.isChecked())
        self.settings.set("sdr_device", self.sdr_device_combo.currentData())
        self.settings.set("audio_device", self.audio_device_combo.currentData())
        self.settings.set("theme", self.theme_combo.currentText())
        self.settings.save()
        self.accept()
        
    def apply_style(self):
        self.setStyleSheet("""
            QDialog { background-color: #0a0a0f; color: #fafafa; }
            QGroupBox { border: 1px solid #2a2a3a; border-radius: 8px; margin-top: 8px; padding: 12px; background-color: #1a1a24; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #a1a1aa; }
            QCheckBox { color: #fafafa; spacing: 8px; }
            QPushButton { background-color: #1a1a24; color: #fafafa; border: 1px solid #2a2a3a; border-radius: 6px; padding: 8px 16px; }
            QPushButton:hover { background-color: #2a2a3a; }
        """)

class FrequencyDialog(QDialog):
    """Dialog to save frequency."""
    
    def __init__(self, current_freq, freq_manager, parent=None):
        super().__init__(parent)
        self.freq_manager = freq_manager
        self.setWindowTitle("Save Frequency")
        self.setMinimumWidth(300)
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel(f"Frequency: {current_freq:.3f} MHz"))
        self.current_freq = current_freq
        
        layout.addWidget(QLabel("Label:"))
        self.label_input = QLineEdit()
        layout.addWidget(self.label_input)
        
        layout.addWidget(QLabel("Description:"))
        self.desc_input = QLineEdit()
        layout.addWidget(self.desc_input)
        
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_freq)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)
        
        self.setStyleSheet("""
            QDialog { background-color: #0a0a0f; color: #fafafa; }
            QLabel { color: #fafafa; }
            QLineEdit { background-color: #1a1a24; color: #fafafa; border: 1px solid #2a2a3a; border-radius: 6px; padding: 6px; }
            QPushButton { background-color: #1a1a24; color: #fafafa; border: 1px solid #2a2a3a; border-radius: 6px; padding: 8px 16px; }
            QPushButton:hover { background-color: #2a2a3a; }
        """)
        
    def save_freq(self):
        label = self.label_input.text().strip()
        if not label:
            QMessageBox.warning(self, "Error", "Label is required")
            return
        
        self.freq_manager.add(self.current_freq, label, self.desc_input.text().strip())
        self.accept()

class AboutDialog(QDialog):
    """About dialog with banner."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About TETRA Decoder Pro")
        self.setMinimumWidth(600)
        self.setMinimumHeight(400)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Load banner from assets
        banner_path = ASSETS_DIR / "banner.png"
        
        if banner_path.exists():
            banner_label = QLabel()
            pixmap = QPixmap(str(banner_path))
            # Scale banner to fit dialog width while maintaining aspect ratio
            if not pixmap.isNull():
                scaled_pixmap = pixmap.scaled(560, 200, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                banner_label.setPixmap(scaled_pixmap)
                banner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(banner_label)
        
        # Version and info
        info_label = QLabel("""
        <h2 style="color: #3b82f6;">TETRA Decoder Pro v2.3</h2>
        <p style="color: #a1a1aa;">Professional TETRA signal decoder with real-time spectrum analysis</p>
        <p style="color: #888888;">Features:</p>
        <ul style="color: #a1a1aa;">
            <li>Real-time TETRA frame decoding</li>
            <li>Waterfall spectrum analyzer</li>
            <li>Voice decoding and playback</li>
            <li>Frequency scanning</li>
            <li>Encryption key management</li>
        </ul>
        """)
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info_label)
        
        layout.addStretch()
        
        # Close button
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setMinimumWidth(100)
        button_layout.addWidget(close_btn)
        layout.addLayout(button_layout)
        
        self.apply_style()
    
    def apply_style(self):
        self.setStyleSheet("""
            QDialog { 
                background-color: #0a0a0f; 
                color: #fafafa; 
            }
            QLabel { 
                color: #fafafa; 
                background-color: transparent;
            }
            QPushButton { 
                background-color: #1a1a24; 
                color: #fafafa; 
                border: 1px solid #2a2a3a; 
                border-radius: 6px; 
                padding: 8px 16px; 
            }
            QPushButton:hover { 
                background-color: #2a2a3a; 
            }
        """)

class WaterfallWidget(QWidget):
    """Waterfall spectrum display widget."""
    
    frequency_clicked = pyqtSignal(float)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(400)
        self.waterfall_data = deque(maxlen=200)  # Keep for resizing
        self.freq_min = 390.0
        self.freq_max = 392.0
        self.full_freq_min = 390.0
        self.full_freq_max = 392.0
        self.power_min = -85  # Bottom threshold per user specification
        self.power_max = 0     # Fixed range
        self.current_fft = None
        self.current_freqs = None
        self.peak_freq = None
        self.peak_power = None
        self.noise_floor = -75  # Noise floor threshold per user specification (lowered)
        self.tuned_freq = None  # Currently tuned frequency
        self.view_center = None  # Optional center frequency for viewport (MHz)
        self.bandwidth = 25000  # Default bandwidth in Hz
        self.mouse_freq = None  # Frequency under mouse cursor
        
        # Zoom and View controls
        self.zoom_level = 1.0
        self.denoiser_enabled = False
        self.smoothed_fft = None
        self.avg_factor = 0.15
        
        # Update throttling — 30 FPS is enough; higher rates starve audio/UI
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(33)  # ~30 FPS
        self.update_timer.timeout.connect(self._do_update)
        self.pending_update = False
        self._last_waterfall_line_t = 0.0
        self._waterfall_line_interval = 0.05  # max ~20 waterfall lines/s
        
        # Cached elements for performance
        self.waterfall_buffer = None  # Persistent buffer for scrolling
        self.grid_cache = None
        self.grid_cache_size = None
        self.grid_cache_params = None
        
        # shadcn/ui dark theme colors
        self.setStyleSheet("background-color: #000000;")
        
        # Enable mouse tracking for hover effects
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        """Handle mouse move for bandwidth overlay (spectrum section only)."""
        width = self.width()
        height = self.height()
        if width <= 0 or height <= 0:
            return
        # Selection bars only while hovering the spectrum analyzer (top ~70%)
        spectrum_height = int(height * 0.7)
        if event.pos().y() > spectrum_height:
            if self.mouse_freq is not None:
                self.mouse_freq = None
                self.update()
            return
        freq_range = self.freq_max - self.freq_min
        self.mouse_freq = self.freq_min + (event.pos().x() / width) * freq_range
        self.update()

    def leaveEvent(self, event):
        """Hide mouse selection bars when the cursor leaves the spectrum widget."""
        if self.mouse_freq is not None:
            self.mouse_freq = None
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        """Handle mouse click to tune."""
        if event.button() == Qt.MouseButton.LeftButton:
            x = event.pos().x()
            width = self.width()
            if width > 0:
                freq_range = self.freq_max - self.freq_min
                freq = self.freq_min + (x / width) * freq_range
                
                # Check for Ctrl key - find peak near click
                modifiers = QApplication.keyboardModifiers()
                if modifiers & Qt.KeyboardModifier.ControlModifier:
                    # Find the spike near the click and snap to its center.
                    # Keep bandwidth unchanged (default 25 kHz) per user preference.
                    spike = self.find_spike_band_near(freq, 50000)
                    freq = spike["center_mhz"]
                    self.center_view_on(freq)
                
                self.frequency_clicked.emit(freq)
    
    def find_peak_near(self, freq, search_range_hz):
        """Find strongest peak near the given frequency."""
        if (
            self.current_fft is None
            or self.current_freqs is None
            or len(self.current_fft) == 0
            or len(self.current_freqs) == 0
        ):
            return freq

        # Search in the underlying FFT bins (not the current viewport mapping).
        half_mhz = search_range_hz / 1e6
        lo = float(freq) - half_mhz
        hi = float(freq) + half_mhz

        freqs = np.asarray(self.current_freqs, dtype=np.float64)
        powers = np.asarray(self.current_fft, dtype=np.float64)
        if freqs.shape != powers.shape:
            n = min(len(freqs), len(powers))
            freqs = freqs[:n]
            powers = powers[:n]

        mask = (freqs >= lo) & (freqs <= hi)
        if not np.any(mask):
            return freq

        idxs = np.where(mask)[0]
        peak_i = int(idxs[np.argmax(powers[idxs])])

        # Sub-bin quadratic interpolation (parabolic fit) for tighter locking.
        # Helps center on narrow spikes even when the true peak falls between FFT bins.
        if 0 < peak_i < len(powers) - 1:
            y1, y2, y3 = powers[peak_i - 1], powers[peak_i], powers[peak_i + 1]
            denom = (y1 - 2 * y2 + y3)
            if denom != 0:
                delta = 0.5 * (y1 - y3) / denom
                # Avoid wild jumps for flat/noisy peaks.
                delta = float(max(-1.0, min(1.0, delta)))
                x1 = freqs[peak_i - 1]
                x2 = freqs[peak_i]
                x3 = freqs[peak_i + 1]
                # Prefer local spacing if non-uniform.
                step = float((x3 - x1) / 2.0) if x3 != x1 else float(x3 - x2)
                return float(x2 + delta * step)

        return float(freqs[peak_i])

    def find_spike_band_near(self, freq_mhz: float, search_range_hz: float) -> dict[str, float]:
        """
        Find the strongest spike near `freq_mhz` and estimate its bandwidth (Hz).

        Returns a dict with:
          - center_mhz
          - left_mhz
          - right_mhz
          - bandwidth_hz

        The spike is defined as the contiguous region around the peak above a dynamic threshold:
          max(peak - 6 dB, local_baseline + 6 dB)
        """
        if (
            self.current_fft is None
            or self.current_freqs is None
            or len(self.current_fft) == 0
            or len(self.current_freqs) == 0
        ):
            return {
                "center_mhz": float(freq_mhz),
                "left_mhz": float(freq_mhz),
                "right_mhz": float(freq_mhz),
                "bandwidth_hz": float(self.bandwidth),
            }

        freqs = np.asarray(self.current_freqs, dtype=np.float64)
        powers = np.asarray(self.current_fft, dtype=np.float64)
        if freqs.shape != powers.shape:
            n = min(len(freqs), len(powers))
            freqs = freqs[:n]
            powers = powers[:n]

        half_mhz = float(search_range_hz) / 1e6
        lo = float(freq_mhz) - half_mhz
        hi = float(freq_mhz) + half_mhz

        mask = (freqs >= lo) & (freqs <= hi)
        if not np.any(mask):
            return {
                "center_mhz": float(freq_mhz),
                "left_mhz": float(freq_mhz),
                "right_mhz": float(freq_mhz),
                "bandwidth_hz": float(self.bandwidth),
            }

        idxs = np.where(mask)[0]
        peak_i = int(idxs[np.argmax(powers[idxs])])
        peak_power = float(powers[peak_i])

        # Local baseline (robust) + dynamic threshold for "spike edges".
        local_powers = powers[idxs]
        local_baseline = float(np.percentile(local_powers, 20))
        threshold = max(peak_power - 6.0, local_baseline + 6.0, float(self.noise_floor) + 3.0)

        left_i = peak_i
        while left_i > idxs[0] and powers[left_i] >= threshold:
            left_i -= 1
        if powers[left_i] < threshold and left_i < peak_i:
            left_i += 1

        right_i = peak_i
        while right_i < idxs[-1] and powers[right_i] >= threshold:
            right_i += 1
        if powers[right_i] < threshold and right_i > peak_i:
            right_i -= 1

        left_mhz = float(freqs[left_i])
        right_mhz = float(freqs[right_i])
        if right_mhz < left_mhz:
            left_mhz, right_mhz = right_mhz, left_mhz

        center_mhz = (left_mhz + right_mhz) / 2.0
        bandwidth_hz = (right_mhz - left_mhz) * 1e6

        # Clamp to a sane range so the overlay remains usable.
        bandwidth_hz = float(max(1000.0, min(250000.0, bandwidth_hz)))

        return {
            "center_mhz": center_mhz,
            "left_mhz": left_mhz,
            "right_mhz": right_mhz,
            "bandwidth_hz": bandwidth_hz,
        }

    def set_bandwidth(self, hz):
        """Set bandwidth for visual overlay."""
        self.bandwidth = hz
        self.update()

    def set_zoom(self, level):
        """Set zoom level (1.0 to 10.0)."""
        self.zoom_level = max(1.0, min(10.0, level))
        self.update_view_range()
        # History was drawn at the previous span — clear so new lines match zoom
        self.waterfall_buffer = None
        self.update()

    def set_denoiser(self, enabled):
        """Enable/disable denoiser."""
        self.denoiser_enabled = enabled
        if not enabled:
            self.smoothed_fft = None

    def update_view_range(self):
        """Update visible frequency range based on zoom.

        The listening channel (tuned_freq) is kept in view whenever possible so
        the red channel bars do not vanish after Tune / zoom / LO settle.
        """
        if self.full_freq_min is not None and self.full_freq_max is not None:
            # Prefer the committed listening frequency as viewport center
            if self.view_center is not None:
                center = self.view_center
            elif self.tuned_freq is not None:
                center = self.tuned_freq
            else:
                center = (self.full_freq_min + self.full_freq_max) / 2
            span = self.full_freq_max - self.full_freq_min
            
            # Apply zoom
            visible_span = span / self.zoom_level

            target_min = center - (visible_span / 2)
            target_max = center + (visible_span / 2)

            # Clamp to available range (keep span constant where possible)
            if target_min < self.full_freq_min:
                shift = self.full_freq_min - target_min
                target_min += shift
                target_max += shift
            if target_max > self.full_freq_max:
                shift = target_max - self.full_freq_max
                target_min -= shift
                target_max -= shift

            self.freq_min = max(self.full_freq_min, target_min)
            self.freq_max = min(self.full_freq_max, target_max)

            # If clamping pushed the listening channel out of view (common right
            # after Tune while FFT still covers the previous LO), shift back.
            if self.tuned_freq is not None and self.freq_max > self.freq_min:
                if self.tuned_freq < self.freq_min or self.tuned_freq > self.freq_max:
                    half = (self.freq_max - self.freq_min) / 2.0
                    self.freq_min = self.tuned_freq - half
                    self.freq_max = self.tuned_freq + half
                    # Stay inside full span if the tuned freq is already covered
                    if self.full_freq_min <= self.tuned_freq <= self.full_freq_max:
                        if self.freq_min < self.full_freq_min:
                            d = self.full_freq_min - self.freq_min
                            self.freq_min += d
                            self.freq_max += d
                        if self.freq_max > self.full_freq_max:
                            d = self.freq_max - self.full_freq_max
                            self.freq_min -= d
                            self.freq_max -= d
                        self.freq_min = max(self.full_freq_min, self.freq_min)
                        self.freq_max = min(self.full_freq_max, self.freq_max)
            
            # Invalidate grid cache
            self.grid_cache = None
            self.grid_cache_params = None
    
    def set_noise_floor(self, value):
        """Set noise floor threshold."""
        self.noise_floor = value
        self.update()
    
    def set_tuned_frequency(self, freq_mhz):
        """Set committed listening frequency for the red channel bars."""
        try:
            freq_mhz = float(freq_mhz)
        except (TypeError, ValueError):
            return
        if not np.isfinite(freq_mhz):
            return
        self.tuned_freq = freq_mhz
        self.view_center = freq_mhz
        self.update_view_range()
        self.update()

    def center_view_on(self, freq_mhz: float) -> None:
        """Center the current viewport on a frequency (MHz) without changing SDR tuning."""
        self.view_center = float(freq_mhz)
        self.update_view_range()
        self.update()
        
    def update_spectrum(self, freqs, powers):
        """Update spectrum data."""
        try:
            self.current_freqs = freqs / 1e6  # Convert to MHz
            
            # Apply denoiser if enabled
            if self.denoiser_enabled:
                if self.smoothed_fft is None or len(self.smoothed_fft) != len(powers):
                    self.smoothed_fft = powers
                else:
                    # IIR Filter
                    self.smoothed_fft = self.smoothed_fft * (1 - self.avg_factor) + powers * self.avg_factor
                self.current_fft = self.smoothed_fft
            else:
                self.current_fft = powers
                self.smoothed_fft = None
            
            # Find peak
            if len(powers) > 0:
                peak_idx = np.argmax(powers)
                self.peak_freq = self.current_freqs[peak_idx]
                self.peak_power = powers[peak_idx]
            
            # Add to waterfall history (for resizing) — cap memory
            self.waterfall_data.append(self.current_fft.copy())
            
            # Update full frequency range
            if len(self.current_freqs) > 0:
                self.full_freq_min = np.min(self.current_freqs)
                self.full_freq_max = np.max(self.current_freqs)
                self.update_view_range()
            
            # Scroll waterfall at most ~20 lines/s (QPainter on every IQ chunk was janky)
            import time as _time
            now = _time.monotonic()
            if now - self._last_waterfall_line_t >= self._waterfall_line_interval:
                self._update_waterfall_buffer()
                self._last_waterfall_line_t = now
            
            # Throttle UI paint via timer
            self.pending_update = True
            if not self.update_timer.isActive():
                self.update_timer.start()
        except Exception as e:
            logger.error(f"Error updating spectrum: {e}")
    
    def _update_waterfall_buffer(self):
        """Update the scrolling waterfall buffer."""
        if self.current_fft is None:
            return
            
        width = self.width()
        height = self.height()
        spectrum_height = int(height * 0.7)
        waterfall_height = height - spectrum_height
        
        if waterfall_height <= 0 or width <= 0:
            return
            
        # Initialize buffer if needed
        if (self.waterfall_buffer is None or 
            self.waterfall_buffer.width() != width or 
            self.waterfall_buffer.height() != waterfall_height):
            self.waterfall_buffer = QImage(width, waterfall_height, QImage.Format.Format_RGB32)
            self.waterfall_buffer.fill(QColor(10, 15, 25))
        
        # Scroll existing content down by 1 pixel
        temp_painter = QPainter(self.waterfall_buffer)
        temp_painter.drawImage(0, 1, self.waterfall_buffer, 0, 0, width, waterfall_height - 1)
        
        # Draw new line at top (y=0), cropped to the same zoomed freq window as the spectrum
        fft_len = len(self.current_fft)
        if fft_len > 0:
            freqs = self.current_freqs
            if (
                freqs is not None
                and len(freqs) == fft_len
                and self.freq_max > self.freq_min
            ):
                # Sample FFT power across the visible [freq_min, freq_max] span
                visible_freqs = np.linspace(self.freq_min, self.freq_max, width)
                resampled = np.interp(visible_freqs, freqs, self.current_fft)
            else:
                # Fallback: full FFT stretched to width (pre-zoom / no freqs yet)
                line_indices = np.linspace(0, fft_len - 1, width, dtype=np.int32)
                resampled = self.current_fft[line_indices]
            
            # Normalize
            power_range = self.power_max - self.power_min
            if power_range > 0:
                normalized = np.clip((resampled - self.power_min) / power_range, 0, 1)
            else:
                normalized = np.zeros_like(resampled)
            
            # Color map (Blue -> Cyan -> Yellow -> Red)
            rgb_line = np.zeros((width, 3), dtype=np.uint8)
            
            # Blue (0-0.25)
            mask1 = normalized < 0.25
            rgb_line[mask1, 2] = np.clip((normalized[mask1] * 4 * 255).astype(np.uint8), 0, 255)
            
            # Blue to Cyan (0.25-0.5)
            mask2 = (normalized >= 0.25) & (normalized < 0.5)
            if np.any(mask2):
                t = (normalized[mask2] - 0.25) * 4
                rgb_line[mask2, 1] = np.clip((t * 255).astype(np.uint8), 0, 255)
                rgb_line[mask2, 2] = 255
            
            # Cyan to Yellow (0.5-0.75)
            mask3 = (normalized >= 0.5) & (normalized < 0.75)
            if np.any(mask3):
                t = (normalized[mask3] - 0.5) * 4
                rgb_line[mask3, 0] = np.clip((t * 255).astype(np.uint8), 0, 255)
                rgb_line[mask3, 1] = 255
                rgb_line[mask3, 2] = np.clip(((1 - t) * 255).astype(np.uint8), 0, 255)
            
            # Yellow to Red (0.75-1.0)
            mask4 = normalized >= 0.75
            if np.any(mask4):
                t = (normalized[mask4] - 0.75) * 4
                rgb_line[mask4, 0] = 255
                rgb_line[mask4, 1] = np.clip(((1 - t) * 255).astype(np.uint8), 0, 255)
            
            # Convert to QImage line — .copy() owns the buffer (avoids UAF/SIGABRT
            # when QImage only wraps a temporary bytes object).
            pixels = (
                0xFF000000
                | (rgb_line[:, 0].astype(np.uint32) << 16)
                | (rgb_line[:, 1].astype(np.uint32) << 8)
                | rgb_line[:, 2].astype(np.uint32)
            )
            buf = np.ascontiguousarray(pixels).tobytes()
            line_img = QImage(buf, width, 1, QImage.Format.Format_RGB32).copy()
            temp_painter.drawImage(0, 0, line_img)
            
        temp_painter.end()
    
    def _do_update(self):
        """Actually trigger the update when timer fires."""
        if self.pending_update:
            self.pending_update = False
            self.update()
    
    def paintEvent(self, event):
        """Paint the traditional SDR-style spectrum and waterfall display."""
        try:
            painter = QPainter(self)
            # Only use antialiasing for spectrum trace, not waterfall
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            
            width = self.width()
            height = self.height()
            
            # Split: 70% spectrum analyzer on top, 30% waterfall on bottom
            spectrum_height = int(height * 0.7)
            waterfall_height = height - spectrum_height
            
            # Fill background with black (matching reference)
            painter.fillRect(0, 0, width, height, QColor(0, 0, 0))
            
            # Draw spectrum analyzer section (top)
            self._draw_spectrum_analyzer(painter, 0, 0, width, spectrum_height)
            
            # Draw waterfall section (bottom)
            if self.waterfall_buffer and not self.waterfall_buffer.isNull():
                # Draw the buffered waterfall image
                target_rect = QRect(0, spectrum_height, width, waterfall_height)
                source_rect = QRect(0, 0, self.waterfall_buffer.width(), self.waterfall_buffer.height())
                painter.drawImage(target_rect, self.waterfall_buffer, source_rect)
            else:
                # Fallback if buffer not ready
                painter.fillRect(0, spectrum_height, width, waterfall_height, QColor(10, 15, 25))
                
        except Exception:
            pass
    
    def _draw_spectrum_analyzer(self, painter, x, y, width, height):
        """Draw spectrum analyzer (top section) - SDR# style."""
        # Background for spectrum - BLACK
        painter.fillRect(x, y, width, height, QColor(0, 0, 0))
        
        freq_range = self.freq_max - self.freq_min
        power_range = self.power_max - self.power_min
        
        # Cache grid lines
        cache_key = (width, height, self.freq_min, self.freq_max, self.power_min, self.power_max, self.noise_floor)
        if self.grid_cache is None or self.grid_cache_params != cache_key:
            self.grid_cache = QPixmap(width, height)
            self.grid_cache.fill(QColor(0, 0, 0, 0))
            grid_painter = QPainter(self.grid_cache)
            
            # Grid lines (Gray)
            grid_painter.setPen(QPen(QColor(60, 60, 60), 1))
            
            # Horizontal grid lines (dBFS scale) - every 10 dB
            if power_range > 0:
                start_db = int(self.power_max / 10) * 10
                for db in range(start_db, int(self.power_min), -10):
                    normalized = (db - self.power_min) / power_range
                    grid_y = int((1 - normalized) * height)
                    grid_painter.drawLine(0, grid_y, width, grid_y)
                    # Label
                    grid_painter.setPen(QPen(QColor(180, 180, 180), 1))
                    grid_painter.drawText(5, grid_y - 2, f"{db}")
                    grid_painter.setPen(QPen(QColor(60, 60, 60), 1))
            
            # Vertical grid lines (frequency)
            if freq_range > 0:
                num_freq_grids = 10
                for i in range(num_freq_grids + 1):
                    freq = self.freq_min + (i * freq_range / num_freq_grids)
                    grid_x = int((freq - self.freq_min) / freq_range * width)
                    grid_painter.drawLine(grid_x, 0, grid_x, height)
            
            grid_painter.end()
            self.grid_cache_params = cache_key
        
        # Draw cached grid
        painter.drawPixmap(x, y, self.grid_cache)
        
        # Draw noise floor threshold line (Yellow dashed)
        if self.noise_floor >= self.power_min and self.noise_floor <= self.power_max and power_range > 0:
            normalized = (self.noise_floor - self.power_min) / power_range
            noise_y = y + int((1 - normalized) * height)
            pen = QPen(QColor(255, 255, 0), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(x, noise_y, x + width, noise_y)
        
        # Draw spectrum trace (Filled Blue + White Outline)
        if self.current_fft is not None and len(self.current_fft) > 0 and len(self.current_freqs) > 0:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            
            path = QPainterPath()
            # Start at bottom left
            path.moveTo(x, y + height)
            
            # Trace points
            for i in range(len(self.current_fft)):
                if i >= len(self.current_freqs):
                    break
                freq = self.current_freqs[i]
                power = self.current_fft[i]
                
                if freq_range > 0:
                    x_pos = x + int(((freq - self.freq_min) / freq_range) * width)
                else:
                    x_pos = x + int((i / len(self.current_fft)) * width)
                
                if power_range > 0:
                    normalized = (power - self.power_min) / power_range
                    # Clamp to view
                    normalized = max(0, min(1, normalized))
                    y_pos = y + int((1 - normalized) * height)
                else:
                    y_pos = y + height
                
                path.lineTo(x_pos, y_pos)
            
            # End at bottom right
            path.lineTo(x + width, y + height)
            path.closeSubpath()
            
            # Fill with gradient (Blue to Transparent) - Subtle
            gradient = QLinearGradient(x, y, x, y + height)
            gradient.setColorAt(0, QColor(0, 100, 255, 50))  # Very transparent
            gradient.setColorAt(1, QColor(0, 50, 150, 10))
            painter.fillPath(path, gradient)
            
            # Draw outline (White) - Thinner, crisper
            pen = QPen(QColor(255, 255, 255), 1.0)
            painter.setPen(pen)
            painter.drawPath(path)
            
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        
        # Listening channel (committed tune) — solid red bars, always drawn when
        # the channel intersects the visible window (clamp partial off-screen).
        if self.tuned_freq is not None and freq_range > 0:
            center_freq = self.tuned_freq
            bw_half = max((float(self.bandwidth) / 1e6) / 2.0, 1e-6)

            def _freq_to_x(freq: float) -> int:
                return x + int(((freq - self.freq_min) / freq_range) * width)

            x_center = _freq_to_x(center_freq)
            x_left = _freq_to_x(center_freq - bw_half)
            x_right = _freq_to_x(center_freq + bw_half)

            band_visible = x_right >= x and x_left <= x + width
            if band_visible:
                pen = QPen(QColor(255, 40, 40), 2)
                painter.setPen(pen)
                # Clamp so a channel near the edge still shows both sides when possible
                left_draw = max(x, min(x + width, x_left))
                right_draw = max(x, min(x + width, x_right))
                painter.drawLine(left_draw, y, left_draw, y + height)
                painter.drawLine(right_draw, y, right_draw, y + height)

                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setWidth(1)
                painter.setPen(pen)
                if x <= x_center <= x + width:
                    painter.drawLine(x_center, y, x_center, y + height)
            
        # Mouse selection overlay (distinct color; hidden on leave via mouse_freq=None)
        if self.mouse_freq is not None and freq_range > 0:
            bw_mhz = max(float(self.bandwidth) / 1e6, 1e-6)
            bw_pixels = max(1, int((bw_mhz / freq_range) * width))
            
            mouse_x = x + int(((self.mouse_freq - self.freq_min) / freq_range) * width)
            
            left_x = mouse_x - (bw_pixels // 2)
            right_x = mouse_x + (bw_pixels // 2)
            
            # Amber — not the listening-channel red, to avoid confusion
            painter.setPen(QPen(QColor(255, 180, 40), 2))
            painter.drawLine(left_x, y, left_x, y + height)
            painter.drawLine(right_x, y, right_x, y + height)
            
            painter.fillRect(left_x, y, bw_pixels, height, QColor(255, 180, 40, 35))
        
        # Draw frequency labels at bottom
        painter.setPen(QPen(QColor(200, 200, 200), 1))
        font = QFont("Arial", 9)
        painter.setFont(font)
        if freq_range > 0:
            num_labels = 5
            for i in range(num_labels):
                freq = self.freq_min + (i * freq_range / (num_labels - 1))
                label_x = x + int((i / (num_labels - 1)) * width)
                # Draw label
                freq_str = f"{freq:.3f}"
                painter.drawText(label_x - 25, y + height - 5, 50, 15, Qt.AlignmentFlag.AlignCenter, freq_str)
    
    def _draw_waterfall(self, painter, x, y, width, height):
        """Draw waterfall spectrogram (bottom section) - optimized with numpy."""
        # Background (shadcn dark)
        painter.fillRect(x, y, width, height, QColor(10, 15, 25))
        
        # Draw waterfall history
        if len(self.waterfall_data) > 0:
            img_width = width - 30  # Leave space for color scale
            img_height = height
            
            # Check if we need to recreate the image
            if (self.waterfall_image is None or 
                self.waterfall_image_size != (img_width, img_height)):
                self.waterfall_image = QImage(img_width, img_height, QImage.Format.Format_RGB32)
                self.waterfall_image_size = (img_width, img_height)
            
            # Fill with background color
            self.waterfall_image.fill(QColor(10, 15, 25))
            
            # Convert waterfall data to numpy array for fast processing
            num_lines = len(self.waterfall_data)
            if num_lines > 0:
                # Get max FFT length
                max_fft_len = max(len(line) for line in self.waterfall_data)
                
                if max_fft_len > 0:
                    # Create 2D array: [num_lines, max_fft_len]
                    waterfall_array = np.full((num_lines, max_fft_len), self.power_min, dtype=np.float32)
                    for i, fft_line in enumerate(self.waterfall_data):
                        if len(fft_line) > 0:
                            waterfall_array[i, :min(len(fft_line), max_fft_len)] = fft_line[:max_fft_len]
                    
                    # Resample to image width using numpy interpolation
                    x_indices = np.linspace(0, max_fft_len - 1, img_width, dtype=np.int32)
                    resampled = waterfall_array[:, x_indices]
                    
                    # Normalize power values
                    power_range = self.power_max - self.power_min
                    if power_range > 0:
                        normalized = np.clip((resampled - self.power_min) / power_range, 0, 1)
                    else:
                        normalized = np.zeros_like(resampled)
                    
                    # Map to image height (reverse y-axis for display - newest at top)
                    y_indices = np.linspace(0, num_lines - 1, img_height, dtype=np.int32)
                    y_indices = np.clip(y_indices, 0, num_lines - 1)
                    
                    # Create RGB array using vectorized color mapping
                    rgb_array = np.zeros((img_height, img_width, 3), dtype=np.uint8)
                    
                    # Process each row
                    for py in range(img_height):
                        line_idx = y_indices[py]
                        if 0 <= line_idx < num_lines:
                            norm_line = normalized[line_idx, :]
                            
                            # Vectorized color mapping using numpy (Blue -> Cyan -> Yellow -> Red)
                            # Blue (0-0.25)
                            mask1 = norm_line < 0.25
                            rgb_array[py, mask1, 2] = np.clip((norm_line[mask1] * 4 * 255).astype(np.uint8), 0, 255)
                            
                            # Blue to Cyan (0.25-0.5)
                            mask2 = (norm_line >= 0.25) & (norm_line < 0.5)
                            if np.any(mask2):
                                t = (norm_line[mask2] - 0.25) * 4
                                rgb_array[py, mask2, 1] = np.clip((t * 255).astype(np.uint8), 0, 255)
                                rgb_array[py, mask2, 2] = 255
                            
                            # Cyan to Yellow (0.5-0.75)
                            mask3 = (norm_line >= 0.5) & (norm_line < 0.75)
                            if np.any(mask3):
                                t = (norm_line[mask3] - 0.5) * 4
                                rgb_array[py, mask3, 0] = np.clip((t * 255).astype(np.uint8), 0, 255)
                                rgb_array[py, mask3, 1] = 255
                                rgb_array[py, mask3, 2] = np.clip(((1 - t) * 255).astype(np.uint8), 0, 255)
                            
                            # Yellow to Red (0.75-1.0)
                            mask4 = norm_line >= 0.75
                            if np.any(mask4):
                                t = (norm_line[mask4] - 0.75) * 4
                                rgb_array[py, mask4, 0] = 255
                                rgb_array[py, mask4, 1] = np.clip(((1 - t) * 255).astype(np.uint8), 0, 255)
                    
                    # Convert numpy RGB array to QImage (QImage uses BGR format)
                    # Create byte array in BGR format
                    bgr_array = rgb_array[:, :, ::-1]  # Reverse RGB to BGR
                    bytes_per_line = img_width * 3
                    img_bytes = bgr_array.tobytes()
                    
                    # Create QImage from bytes
                    self.waterfall_image = QImage(img_bytes, img_width, img_height, bytes_per_line, QImage.Format.Format_RGB888).copy()
            
            painter.drawImage(x, y, self.waterfall_image)
        
        # Draw color scale bar on right
        scale_width = 20
        scale_x = x + width - scale_width
        for py in range(height):
            normalized = 1.0 - (py / height)
            
            # Same color mapping as waterfall (Blue -> Cyan -> Yellow -> Red)
            if normalized < 0.25:
                c = QColor(0, 0, int(normalized * 4 * 255))
            elif normalized < 0.5:
                t = (normalized - 0.25) * 4
                c = QColor(0, int(t * 255), 255)
            elif normalized < 0.75:
                t = (normalized - 0.5) * 4
                c = QColor(int(t * 255), 255, int((1 - t) * 255))
            else:
                t = (normalized - 0.75) * 4
                c = QColor(255, int((1 - t) * 255), 0)
            
            painter.fillRect(scale_x, y + py, scale_width, 1, c)
        
        # Draw scale labels
        painter.setPen(QPen(QColor(200, 200, 200), 1))
        font = QFont("Arial", 8)
        painter.setFont(font)
        painter.drawText(scale_x - 35, y, 30, 12, Qt.AlignmentFlag.AlignRight, f"{int(self.power_max)}")
        painter.drawText(scale_x - 35, y + height - 12, 30, 12, Qt.AlignmentFlag.AlignRight, f"{int(self.power_min)}")


class ScannerDialog(QDialog):
    """Frequency scanner window."""
    
    scan_complete = pyqtSignal(list)  # List of (frequency, power) tuples
    frequency_found = pyqtSignal(float, float, str)  # freq, power, status
    scan_progress = pyqtSignal(int, str)  # progress percent, current frequency
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Frequency Scanner")
        self.setMinimumSize(800, 600)
        self.resize(900, 700)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowMinMaxButtonsHint)
        self.init_ui()
        self.scanner = None
        self.scanning = False
        self.scan_thread = None
        self.apply_dark_theme()
        
        # Connect signals
        self.frequency_found.connect(self.on_frequency_found)
        self.scan_progress.connect(self.on_scan_progress)
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # Title
        title = QLabel("🔍 Frequency Scanner")
        title.setStyleSheet("font-size: 18pt; font-weight: bold; color: #3b82f6; margin-bottom: 5px;")
        layout.addWidget(title)
        
        # Scan range
        range_group = QGroupBox("Scan Parameters")
        range_layout = QVBoxLayout()
        range_layout.setSpacing(8)
        
        # Frequency inputs
        freq_row = QHBoxLayout()
        freq_row.setSpacing(8)
        freq_row.addWidget(QLabel("Start (MHz):"))
        self.start_freq = QLineEdit("390.0")
        self.start_freq.setMinimumWidth(100)
        self.start_freq.setMaximumWidth(120)
        self.start_freq.setPlaceholderText("e.g., 390.0")
        freq_row.addWidget(self.start_freq)
        
        freq_row.addWidget(QLabel("Stop (MHz):"))
        self.stop_freq = QLineEdit("400.0")
        self.stop_freq.setMinimumWidth(100)
        self.stop_freq.setMaximumWidth(120)
        self.stop_freq.setPlaceholderText("e.g., 400.0")
        freq_row.addWidget(self.stop_freq)
        
        freq_row.addWidget(QLabel("Step (kHz):"))
        self.step_freq = QLineEdit("25")
        self.step_freq.setMinimumWidth(80)
        self.step_freq.setMaximumWidth(100)
        self.step_freq.setPlaceholderText("25")
        freq_row.addWidget(self.step_freq)
        
        freq_row.addStretch()
        range_layout.addLayout(freq_row)
        
        # Preset buttons
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        preset_row.addWidget(QLabel("Presets:"))
        
        preset_390 = QPushButton("🇵🇱 Poland (390-395)")
        preset_390.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        preset_390.clicked.connect(lambda: self.set_preset(390.0, 395.0, 25))
        preset_row.addWidget(preset_390)
        
        preset_420 = QPushButton("🇪🇺 Europe (410-430)")
        preset_420.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        preset_420.clicked.connect(lambda: self.set_preset(410.0, 430.0, 25))
        preset_row.addWidget(preset_420)
        
        preset_392 = QPushButton("🎯 Quick (392-393)")
        preset_392.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        preset_392.clicked.connect(lambda: self.set_preset(392.0, 393.0, 25))
        preset_row.addWidget(preset_392)
        
        preset_row.addStretch()
        range_layout.addLayout(preset_row)
        
        range_group.setLayout(range_layout)
        layout.addWidget(range_group)
        
        # Controls
        control_layout = QHBoxLayout()
        control_layout.setSpacing(8)
        self.scan_btn = QPushButton("▶ Start Scan")
        self.scan_btn.setMinimumHeight(42)
        self.scan_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.scan_btn.clicked.connect(self.start_scan)
        control_layout.addWidget(self.scan_btn, 1)
        
        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.setMinimumHeight(42)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.stop_btn.clicked.connect(self.stop_scan)
        control_layout.addWidget(self.stop_btn, 1)
        
        self.close_btn = QPushButton("✖ Close")
        self.close_btn.setMinimumHeight(42)
        self.close_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.close_btn.clicked.connect(self.accept)
        control_layout.addWidget(self.close_btn, 1)
        
        layout.addLayout(control_layout)
        
        # Progress
        progress_layout = QVBoxLayout()
        progress_layout.setSpacing(5)
        self.progress_label = QLabel("Ready to scan")
        self.progress_label.setStyleSheet("color: #3b82f6; font-weight: bold; font-size: 11pt;")
        progress_layout.addWidget(self.progress_label)
        
        self.progress = QProgressBar()
        self.progress.setMinimumHeight(28)
        self.progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        progress_layout.addWidget(self.progress)
        layout.addLayout(progress_layout)
        
        # Results table with scroll area
        results_label = QLabel("📊 Results")
        results_label.setStyleSheet("font-size: 13pt; font-weight: bold; color: #3b82f6; margin-top: 5px;")
        layout.addWidget(results_label)
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(3)
        self.results_table.setHorizontalHeaderLabels(["Frequency (MHz)", "Power (dB)", "Status"])
        
        # Set column resize modes
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        scroll_area.setWidget(self.results_table)
        layout.addWidget(scroll_area, 1)  # Give table stretch factor
    
    def apply_dark_theme(self):
        """Apply shadcn/ui dark theme to scanner."""
        self.setStyleSheet("""
            QDialog {
                background-color: #0a0a0f;
            }
            QWidget {
                background-color: #0a0a0f;
                color: #fafafa;
                font-family: 'Segoe UI', 'Inter', sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #2a2a3a;
                border-radius: 8px;
                margin-top: 8px;
                padding: 12px;
                background-color: #1a1a24;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #a1a1aa;
                font-weight: 600;
                font-size: 11px;
            }
            QPushButton {
                background-color: #1a1a24;
                color: #fafafa;
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2a2a3a;
                border-color: #3a3a4a;
            }
            QPushButton:pressed {
                background-color: #3a3a4a;
            }
            QPushButton:disabled {
                background-color: #0a0a0f;
                color: #52525b;
                border-color: #1a1a24;
            }
            QLineEdit {
                background-color: #1a1a24;
                color: #fafafa;
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                padding: 6px 10px;
                selection-background-color: #3b82f6;
                selection-color: white;
            }
            QLineEdit:focus {
                border: 1px solid #3b82f6;
            }
            QTableWidget {
                background-color: #1a1a24;
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                gridline-color: #2a2a3a;
                alternate-background-color: #0f0f1a;
            }
            QTableWidget::item {
                padding: 6px 8px;
                border: none;
            }
            QTableWidget::item:selected {
                background-color: #2a2a3a;
                color: #fafafa;
            }
            QTableWidget::item:hover {
                background-color: #2a2a3a;
            }
            QHeaderView::section {
                background-color: #0f0f1a;
                color: #a1a1aa;
                padding: 8px 6px;
                border: none;
                border-bottom: 1px solid #2a2a3a;
                border-right: 1px solid #2a2a3a;
                font-weight: 600;
                font-size: 11px;
            }
            QHeaderView::section:hover {
                background-color: #1a1a24;
            }
            QProgressBar {
                background-color: #1a1a24;
                border: 1px solid #2a2a3a;
                border-radius: 4px;
                text-align: center;
                color: #fafafa;
            }
            QProgressBar::chunk {
                background-color: #3b82f6;
                border-radius: 3px;
            }
            QLabel {
                color: #fafafa;
            }
            QScrollArea {
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                background-color: #0a0a0f;
            }
        """)
    
    def set_preset(self, start, stop, step):
        """Set preset values."""
        self.start_freq.setText(str(start))
        self.stop_freq.setText(str(stop))
        self.step_freq.setText(str(step))
    
    def start_scan(self):
        """Start frequency scan."""
        try:
            start = float(self.start_freq.text()) * 1e6
            stop = float(self.stop_freq.text()) * 1e6
            step = float(self.step_freq.text()) * 1e3
            
            from tetraear.signal.capture import RTLCapture
            import time as _time

            parent = self.parent()
            # RTL-SDR is exclusive: an active capture owns the dongle and scan
            # cannot open it ("device not available"). Release it first.
            if (
                parent is not None
                and getattr(parent, "capture_thread", None) is not None
                and parent.capture_thread.isRunning()
            ):
                reply = QMessageBox.question(
                    self,
                    "SDR occupé",
                    "La capture utilise déjà la clé SDR.\n"
                    "Il faut l’arrêter pour scanner.\n\n"
                    "Arrêter la capture et lancer le scan ?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                self.progress_label.setText("Arrêt de la capture (libération SDR)…")
                QApplication.processEvents()
                try:
                    parent.on_stop()
                except Exception as stop_err:
                    QMessageBox.warning(
                        self,
                        "Erreur",
                        f"Impossible de libérer le SDR : {stop_err}",
                    )
                    return
                _time.sleep(0.4)  # USB reopen settle

            sdr_device = None
            if parent is not None:
                if hasattr(parent, "settings_manager"):
                    sdr_device = parent.settings_manager.get("sdr_device", None)
                # Prefer live selection if capture thread was configured
                ct = getattr(parent, "capture_thread", None)
                if ct is not None and getattr(ct, "sdr_device", None) is not None:
                    sdr_device = ct.sdr_device

            rtl = RTLCapture(frequency=start, sample_rate=2.4e6, gain=50, device=sdr_device)
            if not rtl.open():
                QMessageBox.warning(
                    self,
                    "SDR indisponible",
                    "Impossible d’ouvrir la clé RTL-SDR.\n\n"
                    "Vérifiez qu’aucune autre appli (SDR#, GQRX, une autre\n"
                    "instance TetraEar) ne l’utilise, puis réessayez.",
                )
                return
            
            self.scanner = FrequencyScanner(
                rtl, 
                sample_rate=2.4e6,
                scan_step=step,
                noise_floor=-45,
                bottom_threshold=-85,
            )
            self.scan_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.results_table.setRowCount(0)
            self.scanning = True
            
            self.scan_thread = threading.Thread(
                target=self.run_scan, args=(start, stop, step), daemon=True
            )
            self.scan_thread.start()
            
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to start scan: {e}")
            self.scan_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.scanning = False
    
    def run_scan(self, start_freq, stop_freq, step):
        """Two-pass scan: energy peaks vs band noise, then TETRA check on peaks only."""
        try:
            found_count = 0

            def _cancel():
                return not self.scanning

            def _progress(pct, label):
                self.scan_progress.emit(int(pct), label)

            # Pass 1 — channel power vs noise estimated on this band
            self.scan_progress.emit(0, "Pass 1/2 — mesure d'énergie…")
            peaks = self.scanner.find_energy_peaks(
                start_freq,
                stop_freq,
                step,
                snr_db=12.0,
                cancel_check=_cancel,
                progress_cb=_progress,
            )

            if not self.scanning:
                self.scan_progress.emit(0, "Scan arrêté")
                return

            if not peaks:
                self.scan_progress.emit(100, "Aucun pic au-dessus du bruit (+12 dB)")
                return

            # Cap verification work (keep strongest peaks)
            peaks = peaks[:40]
            self.scan_progress.emit(
                50, f"Pass 2/2 — vérif TETRA sur {len(peaks)} pic(s)…"
            )

            for i, peak in enumerate(peaks):
                if not self.scanning:
                    break
                freq = float(peak["frequency"])
                snr = float(peak.get("snr_db", 0.0))
                pct = 50 + int(50 * (i + 1) / max(len(peaks), 1))
                self.scan_progress.emit(pct, f"{freq/1e6:.3f} MHz (vérif)")

                try:
                    result = self.scanner.scan_frequency(freq, dwell_time=0.35)
                except Exception as e:
                    logger.debug("Verify failed at %.3f: %s", freq / 1e6, e)
                    continue

                power = float(result.get("power_db", peak.get("power_db", -100)) or -100)
                is_tetra = bool(result.get("is_tetra", False))
                confidence = float(result.get("confidence", 0.0) or 0.0)
                sync = bool(result.get("sync_detected", False))
                sync_corr = float(result.get("sync_correlation", 0.0) or 0.0)

                if is_tetra and confidence >= 0.30:
                    status = f"TETRA ✓  SNR {snr:.0f} dB  conf {confidence:.0%}"
                elif sync and sync_corr >= 0.45:
                    status = f"Possible TETRA  SNR {snr:.0f} dB  sync {sync_corr:.2f}"
                elif snr >= 15.0:
                    # Strong carrier — still useful to tune even if not classified yet
                    status = f"Porteur fort  SNR {snr:.0f} dB"
                else:
                    continue

                self.frequency_found.emit(freq, power, status)
                found_count += 1

            if self.scanning:
                self.scan_progress.emit(100, f"Terminé — {found_count} candidat(s)")

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.scan_progress.emit(0, f"Scan error: {str(e)}")
        finally:
            if self.scanner and self.scanner.capture:
                try:
                    self.scanner.capture.close()
                except Exception:
                    pass
            self.scanning = False
            QTimer.singleShot(100, self.scan_finished)
    
    def scan_finished(self):
        """Called when scan finishes - updates UI on main thread."""
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
    
    @pyqtSlot(int, str)
    def on_scan_progress(self, progress, current_freq):
        """Update progress bar and current frequency display."""
        self.progress.setValue(progress)
        self.progress_label.setText(f"Scanning {current_freq}... ({progress}%)")
    
    @pyqtSlot(float, float, str)
    def on_frequency_found(self, freq, power, status):
        """Add found frequency to table immediately."""
        self.add_result(freq, power, status)
    
    def stop_scan(self):
        """Stop scanning."""
        self.scanning = False
        if self.scanner and self.scanner.capture:
            try:
                self.scanner.capture.close()
            except:
                pass
        self.progress_label.setText("Scan stopped")
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
    
    def add_result(self, freq, power, status):
        """Add result to table."""
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        
        freq_item = QTableWidgetItem(f"{freq/1e6:.3f}")
        freq_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results_table.setItem(row, 0, freq_item)
        
        power_item = QTableWidgetItem(f"{power:.1f}")
        power_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        # Color code by power
        if power > -50:
            power_item.setForeground(QColor(0, 255, 0))  # Green - strong
        elif power > -70:
            power_item.setForeground(QColor(255, 255, 0))  # Yellow - medium
        else:
            power_item.setForeground(QColor(255, 128, 0))  # Orange - weak
        self.results_table.setItem(row, 1, power_item)
        
        status_item = QTableWidgetItem(status)
        if "TETRA" in status:
            status_item.setForeground(QColor(0, 255, 255))  # Cyan for TETRA
        self.results_table.setItem(row, 2, status_item)


class CaptureThread(QThread):
    """Thread for RTL-SDR capture - simplified to avoid parse errors."""
    
    signal_detected = pyqtSignal(float, float)
    signal_lost = pyqtSignal()
    frame_decoded = pyqtSignal(dict)
    raw_audio_data = pyqtSignal(np.ndarray)
    voice_audio_data = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)
    status_update = pyqtSignal(str)
    spectrum_update = pyqtSignal(np.ndarray, np.ndarray)
    
    def __init__(self):
        super().__init__()
        self.running = False
        self.capture = None
        self.processor = None
        self.decoder = None
        self.voice_processor = None
        self.frequency = 390.865e6  # Default frequency per user request
        self.sample_rate = 2.4e6  # Default sample rate per TETRA spec
        self.gain = 50  # Default gain per user specification
        self.auto_decrypt = True
        self.monitor_raw = False  # New flag
        self.sdr_device = None  # int index, str serial, or None (first available)
        self.pending_freq = None
        self.last_signal_time = 0
        self.encryption_keys = []  # List of keys for bruteforce
        self.tch_assembler = None
        self.samples_per_symbol = 1
        self.demodulated_symbols = None
        self._decode_error_count = 0
        # Optional direct sink for Raw FM (bypass Qt GUI queue — keeps audio realtime)
        self.raw_audio_sink = None
        
    def set_keys(self, keys):
        """Set encryption keys for bruteforce attempts."""
        self.encryption_keys = keys
        if self.decoder:
            self.decoder.set_keys(keys)
            logger.info(f"Loaded {len(keys)} encryption keys into decoder")
        
    def set_monitor_raw(self, enabled):
        self.monitor_raw = enabled

    def set_raw_audio_sink(self, sink):
        """Set a thread-safe callable(audio_float32) for Raw FM (or None)."""
        self.raw_audio_sink = sink
        
    def set_frequency(self, freq):
        """Set tuning frequency."""
        self.pending_freq = freq
    
    def set_gain(self, gain):
        """Set gain."""
        self.gain = gain
        if self.capture and self.capture.sdr:
            try:
                self.capture.sdr.gain = gain
                self.status_update.emit(f"Gain set to {gain}")
            except Exception as e:
                self.error_occurred.emit(f"Failed to set gain: {e}")
    
    def set_sample_rate(self, rate):
        """Set sample rate."""
        # Validate sample rate - RTL-SDR has limited valid rates
        # Valid rates: 0.225, 0.9, 1.024, 1.536, 1.8, 1.92, 2.048, 2.4, 2.56, 2.88, 3.2 MHz
        valid_rates = [0.225e6, 0.9e6, 1.024e6, 1.536e6, 1.8e6, 1.92e6, 2.048e6, 2.4e6, 2.56e6, 2.88e6, 3.2e6]
        closest_rate = min(valid_rates, key=lambda x: abs(x - rate))
        if abs(closest_rate - rate) > 0.1e6:  # More than 100kHz difference
            self.error_occurred.emit(f"Sample rate {rate/1e6:.3f} MHz is not valid, using {closest_rate/1e6:.3f} MHz")
        rate = closest_rate
        
        self.sample_rate = rate
        if self.capture and self.capture.sdr:
            try:
                # RTL-SDR requires restart for sample rate change usually, but let's try
                self.capture.sdr.sample_rate = rate
                if self.processor:
                    self.processor.sample_rate = rate
                self.status_update.emit(f"Sample rate set to {rate/1e6:.1f} MHz")
            except Exception as e:
                self.error_occurred.emit(f"Failed to set sample rate: {e}")
    
    def run(self):
        """Main capture loop - simplified with throttled updates."""
        import time
        self.running = True
        last_spectrum_update = 0
        # 20 Hz max — 200 Hz flooded the GUI thread (choppy waterfall + starved FM ring)
        spectrum_update_interval = 0.05
        last_status_update = 0
        status_update_interval = 0.1  # 10 Hz for status updates
        
        try:
            self.status_update.emit("Initializing RTL-SDR...")
            # RTLCapture accepts 'auto' as string or numeric gain value
            self.capture = RTLCapture(
                frequency=self.frequency,
                sample_rate=self.sample_rate,
                gain=self.gain,
                device=self.sdr_device,
            )
            
            if not self.capture.open():
                self.error_occurred.emit("Failed to open RTL-SDR")
                return
            
            self.processor = SignalProcessor(sample_rate=self.sample_rate)
            self.decoder = TetraDecoder(auto_decrypt=self.auto_decrypt)
            logger.info("Auto-Decrypt: %s", "ON" if self.auto_decrypt else "OFF")
             
            # Pass encryption keys to decoder if we have them
            if self.encryption_keys:
                self.decoder.set_keys(self.encryption_keys)
                logger.info(f"Passed {len(self.encryption_keys)} encryption keys to decoder")
            else:
                logger.info("No user keys loaded (using common keys only)")
             
            self.voice_processor = VoiceProcessor()
            try:
                from tetraear.audio.tch import TchFrameAssembler
                self.tch_assembler = TchFrameAssembler()
            except Exception as e:
                logger.warning("TchFrameAssembler unavailable: %s", e)
                self.tch_assembler = None
            
            self.status_update.emit(f"✓ Started - {self.frequency/1e6:.3f} MHz")
            
            while self.running:
                try:
                    # Handle pending frequency change
                    if self.pending_freq is not None:
                        try:
                            new_freq = self.pending_freq
                            self.pending_freq = None
                            self.frequency = new_freq
                            self.capture.set_frequency(new_freq)
                            # Only emit status if change is large (> 1kHz) to avoid spamming log during AFC
                            # self.status_update.emit(f"Tuned to {new_freq/1e6:.6f} MHz")
                        except Exception as e:
                            self.error_occurred.emit(f"Failed to set frequency: {e}")

                    # Read larger chunk to ensure full TETRA frames (which are ~14ms long)
                    # 32k was too small (~13ms), causing frame splitting and decode failure
                    # 128k is ~54ms (at 2.4MSps), containing ~3-4 full frames
                    # Smaller chunks when Raw FM is on: feed the audio ring often
                    # enough to keep realtime (128k @ 2.4 MSps ≈ 53 ms, but heavy
                    # decode after it was starving the ring → thousands of underruns).
                    read_n = 48 * 1024 if self.monitor_raw else 128 * 1024
                    try:
                        samples = self.capture.read_samples(read_n)
                    except RuntimeError as e:
                        # Device error - stop capture
                        self.error_occurred.emit(f"RTL-SDR device error: {e}. Please restart the application.")
                        self.running = False
                        break

                    # Raw FM FIRST — before FFT/demod/decode — so audio tracks the
                    # SDR clock even when the rest of the pipeline is busy/slow.
                    if self.monitor_raw and samples is not None and len(samples) > 1:
                        try:
                            target_rate = 48000
                            decimation = max(1, int(round(float(self.sample_rate) / target_rate)))
                            audio_samples = samples[::decimation]
                            if len(audio_samples) > 1:
                                audio = np.angle(audio_samples[1:] * np.conj(audio_samples[:-1]))
                                audio = np.asarray(audio, dtype=np.float32)
                                peak = float(np.max(np.abs(audio))) + 1e-9
                                audio = np.clip(audio / peak * 0.7, -1.0, 1.0)
                                self._record_raw_audio(audio, target_rate)
                                # Prefer direct sink (capture thread → ring) so a
                                # busy Qt GUI cannot delay audio and cause underruns.
                                sink = getattr(self, "raw_audio_sink", None)
                                if sink is not None:
                                    try:
                                        sink(audio)
                                    except Exception as sink_err:
                                        audio_logger.debug("Raw FM sink failed: %s", sink_err)
                                else:
                                    self.raw_audio_data.emit(audio)
                        except Exception as audio_err:
                            audio_logger.debug("Raw FM demod failed: %s", audio_err)

                    # Spectrum / detect FFT
                    n_fft = 2048
                    current_time = time.time()
                    power = None
                    freqs = None
                    # Raw FM: FFT only for throttled GUI. Decode mode: FFT every chunk for detect.
                    need_fft = len(samples) >= n_fft and (
                        not self.monitor_raw
                        or (current_time - last_spectrum_update) >= spectrum_update_interval
                    )
                    if need_fft:
                        fft_samples = samples[:n_fft]
                        window = np.hanning(n_fft)
                        fft = np.fft.fftshift(np.fft.fft(fft_samples * window))
                        freqs = np.fft.fftshift(np.fft.fftfreq(n_fft, 1 / self.sample_rate))
                        power = 20 * np.log10(np.abs(fft) / n_fft + 1e-20)
                        if (current_time - last_spectrum_update) >= spectrum_update_interval:
                            self.spectrum_update.emit(freqs + self.frequency, power)
                            last_spectrum_update = current_time

                    # Raw FM listen mode: audio already fed; skip heavy demod this lap
                    if self.monitor_raw:
                        continue
                    
                    # Detect signal (using the computed power)
                    signal_present = False
                    if power is not None and len(samples) >= n_fft:
                        # Calculate average power in the center frequency range (TETRA bandwidth)
                        center_idx = len(power) // 2
                        bandwidth_hz = 25000  # 25 kHz TETRA bandwidth
                        freq_resolution = self.sample_rate / n_fft
                        bandwidth_bins = int(bandwidth_hz / freq_resolution)
                        start_idx = max(0, center_idx - bandwidth_bins // 2)
                        end_idx = min(len(power), center_idx + bandwidth_bins // 2)
                        
                        if end_idx > start_idx:
                            signal_power = np.mean(power[start_idx:end_idx])
                            peak_power = np.max(power[start_idx:end_idx])
                            
                            # Calculate frequency offset for AFC
                            # Find peak bin index
                            peak_idx_local = np.argmax(power[start_idx:end_idx])
                            peak_idx = start_idx + peak_idx_local
                            
                            # Calculate offset from center frequency (0 Hz in baseband)
                            # freqs array is centered at 0
                            peak_freq_offset = freqs[peak_idx]
                            
                            # Calculate noise floor from outside the center channel
                            # Use 100kHz offset to be safe (approx 4x bandwidth)
                            noise_bins_start = 0
                            noise_bins_end = max(0, start_idx - 10)
                            noise_bins_start2 = min(len(power), end_idx + 10)
                            noise_bins_end2 = len(power)
                            
                            noise_power_list = []
                            if noise_bins_end > noise_bins_start:
                                noise_power_list.extend(power[noise_bins_start:noise_bins_end])
                            if noise_bins_end2 > noise_bins_start2:
                                noise_power_list.extend(power[noise_bins_start2:noise_bins_end2])
                                
                            if noise_power_list:
                                noise_floor = np.mean(noise_power_list)
                            else:
                                noise_floor = -100 # Fallback
                                
                            # SNR check
                            snr = signal_power - noise_floor
                            
                            # Use peak power for detection - require clear signal spike
                            detection_power = peak_power
                            
                            # Stricter detection: Require SNR > 15dB AND Peak Power > -70dB
                            # This prevents detecting noise floor or weak interference as signal
                            # Also require peak is significantly above average (at least 3dB) to ensure it's a real spike
                            peak_above_avg = peak_power - signal_power
                            
                            # Signal detection logic
                            is_signal_strong = (snr > 15 and detection_power > -70 and peak_above_avg > 3)
                            
                            if is_signal_strong:
                                self.last_signal_time = current_time
                                signal_present = True
                            
                            # Throttle status updates
                            if current_time - last_status_update >= status_update_interval:
                                if is_signal_strong:
                                    self.signal_detected.emit(self.frequency, signal_power)
                                elif current_time - getattr(self, 'last_signal_time', 0) > 0.5:
                                    # Only emit lost if no signal for 0.5 seconds (hysteresis)
                                    self.signal_lost.emit()
                                last_status_update = current_time
                    
                    # Raw FM mode: skip heavy π/4-DQPSK demod + TETRA decode so the
                    # read→FM→ring loop can keep realtime. Spectrum still updates above.
                    if self.monitor_raw:
                        continue

                    # Only attempt to process and decode TETRA frames when signal is actually present
                    # This prevents false positives from noise or non-TETRA signals
                    if signal_present:
                        try:
                            # Demodulate signal with AFC
                            # Use calculated offset if signal is strong enough, otherwise 0
                            # Lower AFC threshold to -70 dB to help with weaker signals
                            afc_offset = peak_freq_offset if signal_present and peak_power > -70 else 0
                            demodulated = self.processor.process(samples, freq_offset=afc_offset)
                            
                            # Store demodulated symbols for voice extraction
                            # Use the hard-decision symbol stream (0-3) for codec input.
                            self.demodulated_symbols = demodulated if isinstance(demodulated, np.ndarray) else None
                            # Keep complex symbols separately if needed for future diagnostics.
                            if hasattr(self.processor, 'symbols'):
                                self.demodulated_complex = self.processor.symbols
                            elif hasattr(self.processor, 'get_symbols'):
                                self.demodulated_complex = self.processor.get_symbols()
                            
                            # Calculate samples per symbol (typically 4 for π/4-DQPSK at 18k samples/sec)
                            # TETRA symbol rate is 18k symbols/sec, so at 180k samples/sec: 10 samples/symbol
                            symbol_rate = 18000  # TETRA symbol rate
                            self.samples_per_symbol = int(self.sample_rate / symbol_rate) if symbol_rate > 0 else 10
                        
                            # Decode TETRA frames (only when signal is present)
                            # This prevents false positives from noise or non-TETRA signals
                            try:
                                # Ensure we have enough symbols to decode (need at least one frame = 255 symbols)
                                if demodulated is None or len(demodulated) < 255:
                                    frames = []
                                else:
                                    frames = self.decoder.decode(
                                        demodulated,
                                        confidences=getattr(self.processor, "symbol_confidence", None),
                                    )
                                    # Log when frames are found (but limit logging frequency)
                                    if len(frames) > 0:
                                        import time
                                        if not hasattr(self, '_last_frame_log_time'):
                                            self._last_frame_log_time = 0
                                        current_time = time.time()
                                        if current_time - getattr(self, '_last_frame_log_time', 0) > 5.0:
                                            logger.info(f"✓ Decoded {len(frames)} TETRA frame(s) from signal")
                                            self._last_frame_log_time = current_time
                            except Exception as decode_err:
                                self._decode_error_count = getattr(self, "_decode_error_count", 0) + 1
                                if self._decode_error_count <= 5 or self._decode_error_count % 500 == 0:
                                    logger.warning("Decode error: %s", decode_err)
                                frames = []
                            
                            # Emit all decoded frames; voice only from lower-MAC codec_input
                            for frame in frames:
                                    try:
                                        codec_input = frame.get("codec_input")
                                        # Only decode voice after SYNC lock (scrambling known)
                                        sync_locked = bool(
                                            self.decoder
                                            and getattr(self.decoder.lower_mac, "scramb_init", None) is not None
                                        )
                                        if (
                                            codec_input is not None
                                            and len(codec_input) == 1380
                                            and sync_locked
                                            and self.voice_processor
                                            and self.voice_processor.working
                                            # Raw FM already skips this whole demod path above; keep the
                                            # guard for safety. Native in-process decode is cheap, but
                                            # π/4-DQPSK + lower-MAC still costs CPU vs realtime FM.
                                            and not self.monitor_raw
                                        ):
                                            try:
                                                import os
                                                from datetime import datetime
                                                records_dir = str(RECORDS_DIR)
                                                os.makedirs(records_dir, exist_ok=True)
                                                if not getattr(self, "raw_frames_file", None):
                                                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                                    freq_mhz = self.frequency / 1e6
                                                    self.raw_frames_file = os.path.join(
                                                        records_dir,
                                                        f"tetra_frames_{freq_mhz:.3f}MHz_{timestamp}.bin",
                                                    )
                                                    audio_logger.info(
                                                        "Recording raw frames: %s",
                                                        os.path.basename(self.raw_frames_file),
                                                    )
                                                with open(self.raw_frames_file, "ab") as f:
                                                    f.write(codec_input)
                                            except Exception as e:
                                                logger.debug("Failed to save raw frame: %s", e)

                                            audio_segment = self.voice_processor.decode_frame(codec_input)
                                            raw_peak = float(
                                                getattr(self.voice_processor, "last_raw_peak", 0.0) or 0.0
                                            )
                                            verified = bool(frame.get("voice_verified") or frame.get("is_traffic"))
                                            # Unverified (no AACH traffic) needs solid raw energy to avoid noise clicks
                                            accept = len(audio_segment) > 0 and (
                                                verified or raw_peak >= 0.008
                                            )
                                            if accept:
                                                self.voice_audio_data.emit(audio_segment)
                                                frame["has_voice"] = True
                                                logger.debug(
                                                    "[OK] Decoded voice: %s samples frame=%s verified=%s raw_peak=%.5f",
                                                    len(audio_segment),
                                                    frame.get("number"),
                                                    verified,
                                                    raw_peak,
                                                )
                                                audio_logger.debug(
                                                    "Decoded voice: %s samples frame=%s verified=%s raw_peak=%.5f",
                                                    len(audio_segment),
                                                    frame.get("number"),
                                                    verified,
                                                    raw_peak,
                                                )
                                            else:
                                                frame["has_voice"] = False
                                                self._empty_voice_count = getattr(self, "_empty_voice_count", 0) + 1
                                                if self._empty_voice_count <= 5 or self._empty_voice_count % 50 == 0:
                                                    logger.debug(
                                                        "Codec audio rejected frame=%s verified=%s raw_peak=%.5f len=%s (count=%s)",
                                                        frame.get("number"),
                                                        verified,
                                                        raw_peak,
                                                        len(audio_segment),
                                                        self._empty_voice_count,
                                                    )
                                    except Exception as voice_err:
                                        logger.debug("Voice decode error: %s", voice_err)

                                    self.frame_decoded.emit(frame)
                            
                            # Don't generate test frames - only show real decoded frames
                            # Test frames cause false positives and confusion
                    
                        except Exception as decode_err:
                            # Log error but don't spam
                            if not hasattr(self, '_decode_error_logged'):
                                self.error_occurred.emit(f"Decode error: {decode_err}")
                                self._decode_error_logged = True
                    
                except Exception as e:
                    self.error_occurred.emit(f"Capture error: {e}")
                    import traceback
                    traceback.print_exc()
                    break
        
        except Exception as e:
            self.error_occurred.emit(f"Fatal error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.capture:
                self.capture.close()
            self.status_update.emit("Stopped")
    
    def _record_raw_audio(self, audio_data, sample_rate):
        """Record raw FM demodulated audio for offline TETRA decoding."""
        import wave
        import os
        from datetime import datetime
        
        try:
            # Start new recording if not active
            if not hasattr(self, 'raw_audio_recording') or not self.raw_audio_recording:
                records_dir = str(RECORDS_DIR)
                os.makedirs(records_dir, exist_ok=True)
                
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                freq_mhz = self.frequency / 1e6
                self.raw_audio_file = os.path.join(records_dir, f"tetra_raw_{freq_mhz:.4f}MHz.wav")
                
                # Open WAV file for writing (48kHz, 16-bit, mono)
                self.raw_wav_file = wave.open(self.raw_audio_file, 'wb')
                self.raw_wav_file.setnchannels(1)
                self.raw_wav_file.setsampwidth(2)
                self.raw_wav_file.setframerate(sample_rate)
                
                self.raw_audio_recording = True
                audio_logger.info("Started raw audio recording: %s", os.path.basename(self.raw_audio_file))
            
            # Write audio data
            if self.raw_audio_recording and hasattr(self, 'raw_wav_file'):
                audio_int16 = (audio_data * 32767).astype(np.int16)
                self.raw_wav_file.writeframes(audio_int16.tobytes())
                
        except Exception as e:
            logger.debug(f"Raw audio recording error: {e}")
    
    def _stop_raw_audio_recording(self):
        """Stop raw audio recording."""
        if hasattr(self, 'raw_audio_recording') and self.raw_audio_recording:
            try:
                if hasattr(self, 'raw_wav_file'):
                    self.raw_wav_file.close()
                self.raw_audio_recording = False
                audio_logger.info("Stopped raw audio recording: %s", os.path.basename(self.raw_audio_file))
            except Exception as e:
                logger.debug(f"Error stopping raw audio recording: {e}")

    def _extract_voice_slot_from_symbols(self, frame, demodulated_symbols, samples_per_symbol):
        """
        Extract TETRA voice slot from symbol stream for codec.
        Returns soft bits (16-bit integers) in TETRA format (690 shorts = 1380 bytes).
        """
        try:
            from tetraear.audio.tch import extract_tch_codec_input

            pos = frame.get('position')
            return extract_tch_codec_input(demodulated_symbols, pos)
            
        except Exception as e:
            logger.debug(f"Error extracting voice slot: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None
    
    def _generate_synthetic_frame(self):
        """Generate synthetic frame for display (avoids parse errors)."""
        import random
        
        frame_types = ['Broadcast', 'Traffic', 'Control', 'MAC']
        frame_type = random.choice([0, 1, 2, 3])
        
        frame = {
            'number': random.randint(0, 255),
            'type': frame_type,
            'encrypted': random.choice([True, False, False]),  # 33% encrypted
            'decrypted': False,
            'additional_info': {}
        }
        
        # Generate rich descriptions based on frame type
        if frame_type == 0:  # Broadcast
            frame['additional_info']['description'] = random.choice([
                'System broadcast - Network sync',
                'System broadcast - Time sync',
                'System broadcast - Cell info',
                'Network information',
            ])
            frame['additional_info']['network_id'] = random.randint(1000, 9999)
            frame['additional_info']['channel'] = random.choice(['Main', 'Control', 'Backup'])
            
        elif frame_type == 1:  # Traffic
            frame['additional_info']['description'] = random.choice([
                'Traffic channel - Voice call',
                'Traffic channel - Data transfer',
                'Voice transmission',
                'Data packet',
            ])
            frame['additional_info']['channel'] = random.choice(['Voice', 'Data'])
            if random.random() < 0.7:  # 70% have talkgroup
                frame['additional_info']['talkgroup'] = random.randint(1, 9999)
                frame['additional_info']['source_ssi'] = random.randint(100000, 999999)
                frame['additional_info']['dest_ssi'] = random.randint(100000, 999999)
                
        elif frame_type == 2:  # Control
            frame['additional_info']['description'] = random.choice([
                'Control channel - Call setup',
                'Control channel - Call teardown',
                'Registration request',
                'Authentication',
                'Channel assignment',
            ])
            frame['additional_info']['control'] = random.choice([
                'Call setup', 
                'Call teardown', 
                'Registration',
                'Location update',
                'Group call request',
            ])
            if random.random() < 0.5:
                frame['additional_info']['talkgroup'] = random.randint(1, 9999)
                
        elif frame_type == 3:  # MAC
            frame['additional_info']['description'] = random.choice([
                'MAC frame - Access control',
                'MAC frame - Resource grant',
                'Medium access request',
                'Slot allocation',
            ])
        
        # Simulate decryption attempts
        if frame['encrypted'] and random.random() < 0.4:  # 40% success rate
            frame['decrypted'] = True
            frame['key_used'] = random.choice([
                'TEA1 common_key_0 (null)',
                'TEA1 common_key_3 (sequential)',
                'TEA2 common_key_1',
                'TEA1 weak_key',
            ])
            frame['decrypt_confidence'] = random.randint(50, 99)
            frame['decrypted_bytes'] = ''.join(random.choices('0123456789ABCDEF', k=32))
        
        return frame
    
    def stop(self):
        """Stop capture."""
        self.running = False
        # Stop raw audio recording if active
        if hasattr(self, 'raw_audio_recording') and self.raw_audio_recording:
            try:
                if hasattr(self, 'raw_wav_file'):
                    self.raw_wav_file.close()
                self.raw_audio_recording = False
                audio_logger.info("Stopped raw audio recording")
            except Exception as e:
                logger.debug(f"Error stopping raw audio recording: {e}")


class ModernTetraGUI(QMainWindow):
    """Modern TETRA decoder GUI."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TetraEar - Professional TETRA Decoder v2.3")
        self.setGeometry(100, 100, 1600, 1100)
        
        # Set application icon
        self.set_app_icon()

        self.capture_thread = None
        self.frame_count = 0
        self.decrypted_count = 0
        self.scanner_dialog = None
        self.encryption_keys = []
        self.auto_spectrum_enabled = False
        self._last_auto_spectrum_time = 0.0
        
        # Filter data sets
        self.known_call_groups = set()
        self.known_call_clients = set()
        self.known_user_groups = set()
        
        # TETRA signal detection tracking
        self.tetra_sync_count = 0
        self.tetra_frame_count = 0
        self.tetra_valid_frames = 0
        self.last_tetra_update = 0.0
        self.signal_present = False  # Track if signal is currently present
        self.first_frame_time = None  # Track when first frame was detected
        self.min_detection_time = 5.0  # Minimum seconds before showing "TETRA Signal Detected"
        self.cell_mcc = None
        self.cell_mnc = None
        self.cell_colour = None
        self.cell_la = None
        
        # SDS reassembly tracking
        self.sds_fragments = {}  # Key: (src, dst, msg_id), Value: {fragments, timestamp}
        self.sds_timeout = 30  # seconds
        
        # Continuous audio recording
        self.continuous_recording = True
        self.audio_buffer = []
        self.recording_active = False
        self.recording_enabled = False  # Manual recording toggle
        self.recording_start_time = None
        self.recording_has_audio = False  # Track if valid audio was recorded
        self.tch_assembler = None
        self._audio_q = queue.Queue(maxsize=64)
        self._audio_player_stop = threading.Event()
        self._audio_player_thread = None
        self._audio_out_rate = 48000
        self._audio_play_lock = threading.Lock()
        self.audio_stream = None  # unused legacy; kept for compatibility
        # Continuous Raw-FM stream (ring + OutputStream) — avoids choppy sd.play bursts
        self._raw_stream = None
        self._raw_ring = None
        self._raw_ring_lock = threading.Lock()
        self._raw_ring_cap = 0
        self._raw_ring_data = None
        self._raw_ring_r = 0
        self._raw_ring_w = 0
        self._raw_ring_size = 0
        self._raw_underruns = 0
        self._raw_drops = 0
        
        # Managers
        self.settings_manager = SettingsManager()
        self.freq_manager = FrequencyManager()
        
        # TETRA signal validator (optionally constrain by expected MCC)
        expected_mcc = self.settings_manager.get("expected_mcc", None)
        self.signal_validator = TetraSignalValidator(expected_country_mcc=expected_mcc)

        try:
            from tetraear.audio.tch import TchFrameAssembler
            self.tch_assembler = TchFrameAssembler()
        except Exception:
            self.tch_assembler = None
        
        self.init_ui()
        self.apply_modern_style()
        
        # Load settings
        self.load_settings()
        
        # Update timer
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_displays)
        self.update_timer.start(100)
        
        # Initialize audio
        self.init_audio()

        # Column widths are fixed/Interactive — do NOT periodically resizeToContents
        # (that made tables jump every time new frames arrived).
    
    def load_settings(self):
        """Load initial settings."""
        s = self.settings_manager
        self.freq_input.setText(str(s.get("last_frequency", 390.865)))
        self.auto_decrypt_cb.setChecked(s.get("auto_decrypt", True))
        self.hear_voice_cb.setChecked(s.get("monitor_audio", True))
        self.monitor_raw_cb.setChecked(s.get("monitor_raw", False))
        
        # Update presets
        self.update_presets()

    def update_presets(self):
        """Update preset combo box."""
        self.freq_preset.clear()
        self.freq_preset.addItem("Custom")
        for item in self.freq_manager.get_all():
            self.freq_preset.addItem(f"{item['freq']:.3f} MHz - {item['label']}")

    def open_settings(self):
        """Open settings dialog."""
        dlg = SettingsDialog(self.settings_manager, self)
        if dlg.exec():
            # Apply settings immediately if needed
            pass

    def save_current_freq(self):
        """Open save frequency dialog."""
        try:
            freq = float(self.freq_input.text())
            dlg = FrequencyDialog(freq, self.freq_manager, self)
            if dlg.exec():
                self.update_presets()
        except ValueError:
            pass
    
    def show_about(self):
        """Show about dialog with banner."""
        dlg = AboutDialog(self)
        dlg.exec()

    def set_app_icon(self):
        """Load and set application icon from assets folder."""
        # Try to load icon from assets folder
        icon_paths = [
            ASSETS_DIR / "icon.ico",
            ASSETS_DIR / "icon_preview.png",
            ASSETS_DIR / "icon.png"
        ]
        
        icon_loaded = False
        for icon_path in icon_paths:
            if icon_path.exists():
                try:
                    icon = QIcon(str(icon_path))
                    if not icon.isNull():
                        self.setWindowIcon(icon)
                        logger.debug(f"Loaded icon from: {icon_path}")
                        icon_loaded = True
                        break
                except Exception as e:
                    logger.debug(f"Failed to load icon from {icon_path}: {e}")
        
        # Fallback: create icon programmatically if assets not found
        if not icon_loaded:
            logger.debug("Creating fallback icon programmatically")
            size = 64
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            # Draw background circle
            painter.setBrush(QBrush(QColor(40, 60, 100)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(2, 2, size-4, size-4)
            
            # Draw signal wave
            path = QPainterPath()
            path.moveTo(10, size/2)
            path.cubicTo(size/3, size/4, 2*size/3, 3*size/4, size-10, size/2)
            
            pen = QPen(QColor(0, 255, 255))
            pen.setWidth(4)
            painter.setPen(pen)
            painter.drawPath(path)
            
            # Draw text
            painter.setPen(QColor(255, 255, 255))
            font = QFont("Arial", 10, QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "TETRA")
            
            painter.end()
            
            self.setWindowIcon(QIcon(pixmap))

    def init_ui(self):
        """Initialize UI with compact layout."""
        central = QWidget()
        self.setCentralWidget(central)
        # Use QSplitter for resizable panels
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.setChildrenCollapsible(False)
        self._main_splitter = main_splitter
        
        # Top controls - with proper size policy
        control_panel = self.create_control_panel()
        control_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        control_panel.setMinimumHeight(300)
        self._control_panel = control_panel
        main_splitter.addWidget(control_panel)
        
        # Waterfall spectrum - with proportional sizing
        spectrum_group = QGroupBox("Spectrum Analyzer")
        spectrum_layout = QVBoxLayout()
        spectrum_layout.setContentsMargins(5, 5, 5, 5)
        self.waterfall = WaterfallWidget()
        self.waterfall.setMinimumHeight(200)
        self.waterfall.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        spectrum_layout.addWidget(self.waterfall)
        spectrum_group.setLayout(spectrum_layout)
        spectrum_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        main_splitter.addWidget(spectrum_group)
        
        # Tabs - with scroll area for table
        tabs = QTabWidget()
        
        # Frames tab with scrollable table
        frames_widget = self.create_frames_tab()
        tabs.addTab(frames_widget, "📡 Decoded Frames")
        
        # Calls tab
        calls_widget = self.create_calls_tab()
        tabs.addTab(calls_widget, "📞 Calls")
        
        # Groups tab
        groups_widget = self.create_groups_tab()
        tabs.addTab(groups_widget, "👥 Groups")
        
        # Users tab
        users_widget = self.create_users_tab()
        tabs.addTab(users_widget, "👤 Users")
        
        # Log tab
        log_widget = QWidget()
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(5, 5, 5, 5)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_text)
        log_widget.setLayout(log_layout)
        tabs.addTab(log_widget, "📋 Log")
        
        # Stats tab
        stats_widget = self.create_stats_tab()
        tabs.addTab(stats_widget, "📊 Statistics")
        
        tabs.setMinimumHeight(200)
        main_splitter.addWidget(tabs)
        
        # Set initial sizes after layout is realized (make sure the full control panel is visible).
        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setStretchFactor(2, 1)
        QTimer.singleShot(0, self._apply_initial_splitter_sizes)
        
        # Add splitter to main layout
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.addWidget(main_splitter)
        
        # Status bar
        self.statusBar().showMessage("Ready")

    def _apply_initial_splitter_sizes(self) -> None:
        splitter = getattr(self, "_main_splitter", None)
        control_panel = getattr(self, "_control_panel", None)
        if splitter is None or control_panel is None:
            return

        total_h = splitter.size().height()
        if total_h <= 0:
            total_h = self.height()

        # Aim to show all top control groups without scrolling.
        control_hint = control_panel.sizeHint().height()
        control_target = min(max(control_hint + 40, 520), int(total_h * 0.62))

        # Keep spectrum and tabs usable too.
        spectrum_min = 220
        tabs_min = 260
        remaining = max(0, total_h - control_target)

        spectrum_target = max(spectrum_min, int(remaining * 0.45))
        tabs_target = max(tabs_min, remaining - spectrum_target)

        splitter.setSizes([control_target, spectrum_target, tabs_target])
    
    def create_control_panel(self):
        """Create modern control panel with redesigned layout."""
        # Main container with horizontal layout - compact
        main_widget = QWidget()
        main_layout = QHBoxLayout(main_widget)
        main_layout.setSpacing(12)  # Increased spacing
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # Left column: Frequency and tuning - compact
        left_col = QVBoxLayout()
        left_col.setSpacing(8)  # Increased spacing
        
        # Frequency group
        freq_group = QGroupBox("Frequency")
        freq_layout = QVBoxLayout()
        freq_layout.setSpacing(6)
        freq_row1 = QHBoxLayout()
        freq_label = QLabel("Frequency (MHz):")
        freq_label.setMinimumWidth(100)
        freq_row1.addWidget(freq_label)
        self.freq_input = QLineEdit("390.865")  # Default frequency per user specification
        self.freq_input.setMinimumWidth(100)
        self.freq_input.setMaximumWidth(150)
        self.freq_input.setPlaceholderText("390.000")
        freq_row1.addWidget(self.freq_input, 1)  # Stretch factor
        
        # Save Freq Button
        self.save_freq_btn = QPushButton("💾")
        self.save_freq_btn.setToolTip("Save Frequency")
        self.save_freq_btn.setMaximumWidth(40)
        self.save_freq_btn.clicked.connect(self.save_current_freq)
        freq_row1.addWidget(self.save_freq_btn)
        
        self.tune_btn = QPushButton("⚡ Tune")
        self.tune_btn.clicked.connect(self.on_tune)
        self.tune_btn.setMinimumWidth(80)
        freq_row1.addWidget(self.tune_btn)
        freq_layout.addLayout(freq_row1)
        
        freq_row2 = QHBoxLayout()
        preset_label = QLabel("Preset:")
        preset_label.setMinimumWidth(100)
        freq_row2.addWidget(preset_label)
        self.freq_preset = QComboBox()
        # Items populated by update_presets()
        self.freq_preset.currentTextChanged.connect(self.on_preset_changed)
        freq_row2.addWidget(self.freq_preset, 1)  # Stretch factor
        freq_layout.addLayout(freq_row2)
        
        # Bandwidth input
        bw_row = QHBoxLayout()
        bw_label = QLabel("Bandwidth (Hz):")
        bw_label.setMinimumWidth(100)
        bw_row.addWidget(bw_label)
        self.bw_input = QLineEdit("25000")
        self.bw_input.setMinimumWidth(100)
        self.bw_input.setMaximumWidth(150)
        self.bw_input.textChanged.connect(self.on_bandwidth_changed)
        bw_row.addWidget(self.bw_input, 1)
        freq_layout.addLayout(bw_row)
        
        freq_group.setLayout(freq_layout)
        left_col.addWidget(freq_group)
        
        # Action buttons
        button_group = QGroupBox("Actions")
        button_layout = QVBoxLayout()
        button_layout.setSpacing(6)
        button_row1 = QHBoxLayout()
        button_row1.setSpacing(6)
        self.start_btn = QPushButton("▶ START")
        self.start_btn.clicked.connect(self.on_start)
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        button_row1.addWidget(self.start_btn, 1)  # Stretch factor
        
        self.stop_btn = QPushButton("⏹ STOP")
        self.stop_btn.clicked.connect(self.on_stop)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        button_row1.addWidget(self.stop_btn, 1)  # Stretch factor
        
        self.record_btn = QPushButton("🔴 REC")
        self.record_btn.setCheckable(True)
        self.record_btn.clicked.connect(self.toggle_recording)
        self.record_btn.setMinimumHeight(40)
        self.record_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.record_btn.setStyleSheet("QPushButton:checked { background-color: #7f1d1d; border-color: #ef4444; }")
        button_row1.addWidget(self.record_btn, 1)
        
        button_layout.addLayout(button_row1)
        
        button_row2 = QHBoxLayout()
        button_row2.setSpacing(6)
        self.scan_btn = QPushButton("🔍 SCAN")
        self.scan_btn.clicked.connect(self.on_scan)
        self.scan_btn.setMinimumHeight(36)
        self.scan_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        button_row2.addWidget(self.scan_btn, 1)  # Stretch factor
        
        self.load_keys_btn = QPushButton("🔑 Load Keys")
        self.load_keys_btn.clicked.connect(self.on_load_keys)
        self.load_keys_btn.setMinimumHeight(36)
        self.load_keys_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        button_row2.addWidget(self.load_keys_btn, 1)  # Stretch factor
        
        # Settings Button
        self.settings_btn = QPushButton("⚙️ Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        self.settings_btn.setMinimumHeight(36)
        self.settings_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        button_row2.addWidget(self.settings_btn, 1)
        
        # About button
        about_btn = QPushButton("ℹ About")
        about_btn.clicked.connect(self.show_about)
        about_btn.setMinimumHeight(36)
        about_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        button_row2.addWidget(about_btn, 1)
        
        button_layout.addLayout(button_row2)

        # Moved Spectrum Display toggles here for easier access
        toggles_layout = QVBoxLayout()
        toggles_layout.setSpacing(4)

        self.auto_spectrum_cb = QCheckBox("Auto Spectrum Sliders")
        self.auto_spectrum_cb.setToolTip("Dynamically adjust zoom, top, bottom, and threshold based on live signal")
        self.auto_spectrum_cb.toggled.connect(self.on_auto_spectrum_toggled)
        toggles_layout.addWidget(self.auto_spectrum_cb)

        self.denoiser_cb = QCheckBox("Denoiser (Smooth)")
        self.denoiser_cb.toggled.connect(self.on_denoiser_toggled)
        toggles_layout.addWidget(self.denoiser_cb)

        # Auto frequency correction (spike locking)
        self.follow_freq_cb = QCheckBox("Auto-Follow Spike (AFC)")
        self.follow_freq_cb.setChecked(False)
        self.follow_freq_cb.setToolTip("Automatically adjust tuning to keep the main spike centered")
        toggles_layout.addWidget(self.follow_freq_cb)

        button_layout.addLayout(toggles_layout)
        button_group.setLayout(button_layout)
        left_col.addWidget(button_group)
        
        left_col.addStretch()
        main_layout.addLayout(left_col, 1)  # Stretch factor
        
        # Middle column: Gain and Sample Rate - compact
        middle_col = QVBoxLayout()
        middle_col.setSpacing(6)
        
        # Gain slider
        gain_group = QGroupBox("Gain")
        gain_layout = QVBoxLayout()
        gain_row = QHBoxLayout()
        gain_row.setSpacing(8)
        gain_label = QLabel("Gain:")
        gain_label.setMinimumWidth(50)
        gain_row.addWidget(gain_label)
        self.gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.gain_slider.setMinimum(0)
        self.gain_slider.setMaximum(100)  # 0-50 with 0.5 steps
        self.gain_slider.setValue(100)  # Default 50.0 (100/2 = 50)
        self.gain_slider.valueChanged.connect(self.on_gain_slider_changed)
        self.gain_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        gain_row.addWidget(self.gain_slider, 1)  # Stretch factor
        self.gain_label = QLabel("50.0 dB")
        self.gain_label.setMinimumWidth(70)
        self.gain_label.setMaximumWidth(80)
        self.gain_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.gain_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        gain_row.addWidget(self.gain_label)
        gain_layout.addLayout(gain_row)
        gain_group.setLayout(gain_layout)
        middle_col.addWidget(gain_group)
        
        # Sample Rate slider - with 0.1 MHz steps from 1.8 to 10.0 MHz
        sample_rate_group = QGroupBox("Sample Rate")
        sample_rate_layout = QVBoxLayout()
        sample_rate_row = QHBoxLayout()
        sample_rate_row.setSpacing(8)
        rate_label = QLabel("Rate:")
        rate_label.setMinimumWidth(50)
        sample_rate_row.addWidget(rate_label)
        self.sample_rate_slider = QSlider(Qt.Orientation.Horizontal)
        # RTL-SDR valid rates: 0.225, 0.9, 1.024, 1.536, 1.8, 1.92, 2.048, 2.4, 2.56, 2.88, 3.2 MHz
        # Map slider to most common rates: 1.8, 1.92, 2.048, 2.4, 2.56, 2.88, 3.2
        valid_rates_mhz = [1.8, 1.92, 2.048, 2.4, 2.56, 2.88, 3.2]
        self.valid_sample_rates = valid_rates_mhz
        self.sample_rate_slider.setMinimum(0)
        self.sample_rate_slider.setMaximum(len(valid_rates_mhz) - 1)
        self.sample_rate_slider.setValue(3)  # Default 2.4 MHz (index 3)
        self.sample_rate_slider.setSingleStep(1)
        self.sample_rate_slider.valueChanged.connect(self.on_sample_rate_slider_changed)
        self.sample_rate_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sample_rate_row.addWidget(self.sample_rate_slider, 1)  # Stretch factor
        self.sample_rate_label = QLabel("2.4 MHz")
        self.sample_rate_label.setMinimumWidth(70)
        self.sample_rate_label.setMaximumWidth(80)
        self.sample_rate_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sample_rate_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        sample_rate_row.addWidget(self.sample_rate_label)
        sample_rate_layout.addLayout(sample_rate_row)
        sample_rate_group.setLayout(sample_rate_layout)
        middle_col.addWidget(sample_rate_group)
        
        # Options group (Moved from right column)
        options_group = QGroupBox("Options")
        options_layout = QVBoxLayout()
        self.auto_decrypt_cb = QCheckBox("Auto-Decrypt")
        self.auto_decrypt_cb.setChecked(True)
        self.auto_decrypt_cb.toggled.connect(self.on_auto_decrypt_toggled)
        options_layout.addWidget(self.auto_decrypt_cb)
        
        self.hear_voice_cb = QCheckBox("🔊 Monitor Audio")
        self.hear_voice_cb.setChecked(True)
        self.hear_voice_cb.setToolTip("Listen to decoded voice (if available)")
        self.hear_voice_cb.toggled.connect(self._on_monitor_audio_toggled)
        options_layout.addWidget(self.hear_voice_cb)

        self.test_speaker_btn = QPushButton("Test Speaker")
        self.test_speaker_btn.setToolTip("Play a short beep on the selected audio output")
        self.test_speaker_btn.clicked.connect(self._test_speaker)
        options_layout.addWidget(self.test_speaker_btn)
        
        self.monitor_raw_cb = QCheckBox("📻 Raw Signal (FM)")
        self.monitor_raw_cb.setChecked(False)
        self.monitor_raw_cb.setToolTip("Listen to raw FM demodulated audio (debug)")
        self.monitor_raw_cb.toggled.connect(self.on_monitor_raw_toggled)
        options_layout.addWidget(self.monitor_raw_cb)
        
        options_group.setLayout(options_layout)
        middle_col.addWidget(options_group)
        
        middle_col.addStretch()
        main_layout.addLayout(middle_col, 1)  # Stretch factor
        
        # Right column: Spectrum Display Controls
        right_col = QVBoxLayout()
        right_col.setSpacing(8)
        
        # Display Controls Group (Combined with Noise Floor)
        display_group = QGroupBox("Spectrum Display")
        display_layout = QVBoxLayout()
        display_layout.setSpacing(14)  # Increased spacing between sliders
        display_layout.setContentsMargins(10, 20, 10, 14)  # Add more top/bottom margin
        
        # Zoom Slider
        zoom_row = QHBoxLayout()
        zoom_label = QLabel("Zoom:")
        zoom_label.setMinimumWidth(60)
        zoom_row.addWidget(zoom_label)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setMinimum(10)
        self.zoom_slider.setMaximum(100)
        self.zoom_slider.setValue(10)
        self.zoom_slider.valueChanged.connect(self.on_zoom_changed)
        zoom_row.addWidget(self.zoom_slider)
        display_layout.addLayout(zoom_row)
        
        # Range Slider (Top dB)
        range_row = QHBoxLayout()
        range_label = QLabel("Top:")
        range_label.setMinimumWidth(60)
        range_row.addWidget(range_label)
        self.range_slider = QSlider(Qt.Orientation.Horizontal)
        self.range_slider.setMinimum(-100)
        self.range_slider.setMaximum(20)
        self.range_slider.setValue(0)
        self.range_slider.valueChanged.connect(self.on_range_changed)
        range_row.addWidget(self.range_slider)
        
        self.range_label = QLabel("0 dB")
        self.range_label.setMinimumWidth(50)
        range_row.addWidget(self.range_label)
        
        display_layout.addLayout(range_row)
        
        # Noise Floor Slider (Visual Bottom)
        noise_floor_row = QHBoxLayout()
        threshold_label = QLabel("Bottom:")
        threshold_label.setMinimumWidth(60)
        noise_floor_row.addWidget(threshold_label)
        self.noise_floor_slider = QSlider(Qt.Orientation.Horizontal)
        self.noise_floor_slider.setMinimum(-140)
        self.noise_floor_slider.setMaximum(-40)
        self.noise_floor_slider.setValue(-85)  # Default bottom threshold per user specification
        self.noise_floor_slider.valueChanged.connect(self.on_noise_floor_changed)
        noise_floor_row.addWidget(self.noise_floor_slider)
        
        self.noise_floor_label = QLabel("-85 dB")
        self.noise_floor_label.setMinimumWidth(50)
        noise_floor_row.addWidget(self.noise_floor_label)
        
        display_layout.addLayout(noise_floor_row)
        
        # Threshold Slider (Yellow Line)
        threshold_row = QHBoxLayout()
        threshold_label = QLabel("Threshold:")
        threshold_label.setMinimumWidth(60)
        threshold_row.addWidget(threshold_label)
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setMinimum(-100)
        self.threshold_slider.setMaximum(-20)
        self.threshold_slider.setValue(-75)  # Default threshold
        self.threshold_slider.valueChanged.connect(self.on_threshold_changed)
        threshold_row.addWidget(self.threshold_slider)
        
        self.threshold_label = QLabel("-75 dB")
        self.threshold_label.setMinimumWidth(50)
        threshold_row.addWidget(self.threshold_label)
        
        display_layout.addLayout(threshold_row)
        
        # (spectrum display toggles moved to Actions panel)
        
        display_group.setLayout(display_layout)
        right_col.addWidget(display_group)
        
        # Status indicators (Moved from right column bottom)
        status_group = QGroupBox("Status")
        status_layout = QVBoxLayout() # Changed to vertical for TETRA status
        status_layout.setSpacing(12)
        status_layout.setContentsMargins(10, 20, 10, 14)
        
        status_row1 = QHBoxLayout()
        self.signal_label = QLabel("⚫ No Signal")
        self.signal_label.setStyleSheet("font-weight: bold; padding: 5px;")
        status_row1.addWidget(self.signal_label)
        
        self.decrypt_label = QLabel("🔒 0/0")
        self.decrypt_label.setStyleSheet("font-weight: bold; padding: 5px;")
        status_row1.addWidget(self.decrypt_label)
        status_layout.addLayout(status_row1)
        
        # Recording status
        self.recording_status_label = QLabel("⚫ Not Recording")
        self.recording_status_label.setStyleSheet("font-weight: bold; padding: 5px; color: #888888;")
        status_layout.addWidget(self.recording_status_label)
        
        # TETRA signal detection status
        self.tetra_status_label = QLabel("⚫ No TETRA Signal")
        self.tetra_status_label.setStyleSheet(
            "font-weight: bold; padding: 5px; color: #888888;"
        )
        status_layout.addWidget(self.tetra_status_label)
        
        status_group.setLayout(status_layout)
        right_col.addWidget(status_group)
        
        right_col.addStretch()
        main_layout.addLayout(right_col, 1)  # Stretch factor
        
        return main_widget
    
    def create_frames_tab(self):
        """Create frames tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Controls
        control_layout = QHBoxLayout()
        
        # Filter
        control_layout.addWidget(QLabel("Filter:"))
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All Types", "Traffic", "Control", "Broadcast", "MAC", "SDS", "Audio"])
        self.type_filter.currentTextChanged.connect(self.apply_filter)
        control_layout.addWidget(self.type_filter)
        
        self.decrypted_only_cb = QCheckBox("Decrypted/Text Only")
        self.decrypted_only_cb.toggled.connect(lambda: self.apply_filter(self.type_filter.currentText()))
        control_layout.addWidget(self.decrypted_only_cb)
        
        control_layout.addSpacing(20)
        
        self.autoscroll_cb = QCheckBox("Auto-scroll")
        self.autoscroll_cb.setChecked(False)
        control_layout.addWidget(self.autoscroll_cb)
        
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda: self.frames_table.setRowCount(0))
        control_layout.addWidget(clear_btn)
        
        control_layout.addStretch()
        layout.addLayout(control_layout)
        
        # Table (no scroll area wrapper needed for QTableWidget)
        self.frames_table = QTableWidget()
        self.frames_table.setColumnCount(9)  # Added Country column
        self.frames_table.setHorizontalHeaderLabels([
            "⏱ Time", "# Frame", "📋 Type", "📝 Description", "💬 Message", "🔐 Encrypted", "✅ Status", "📊 Data", "🌍 Country"
        ])
        # Stable column widths — Interactive so the user can drag; never ResizeToContents
        self.frames_table.setColumnWidth(0, 80)
        self.frames_table.setColumnWidth(1, 70)
        self.frames_table.setColumnWidth(2, 120)
        self.frames_table.setColumnWidth(3, 280)
        self.frames_table.setColumnWidth(4, 220)
        self.frames_table.setColumnWidth(5, 90)
        self.frames_table.setColumnWidth(6, 160)
        self.frames_table.setColumnWidth(7, 220)
        self.frames_table.setColumnWidth(8, 140)

        header = self.frames_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        
        # Set table properties
        self.frames_table.setAlternatingRowColors(True)
        self.frames_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.frames_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.frames_table.setShowGrid(False)
        self.frames_table.verticalHeader().setVisible(False)
        self.frames_table.verticalHeader().setDefaultSectionSize(24)
        self.frames_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.frames_table.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.frames_table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.frames_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.frames_table.setWordWrap(False)
        self.frames_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        
        # Add tooltips to headers
        headers = ["Time", "Frame #", "Type", "Description", "Message", "Encrypted", "Status", "Data"]
        tooltips = [
            "Timestamp when frame was decoded",
            "TETRA frame number (0-255)",
            "Frame type (MAC-RESOURCE, MAC-BROADCAST, etc.)",
            "Frame description with metadata (TG, SSI, etc.)",
            "Decoded SDS/text snippet (if available)",
            "Whether frame is encrypted",
            "Decryption status and key information",
            "Frame payload data (hex or text)"
        ]
        for i, (header, tooltip) in enumerate(zip(headers, tooltips)):
            item = self.frames_table.horizontalHeaderItem(i)
            if item:
                item.setToolTip(tooltip)
        
        layout.addWidget(self.frames_table)
        
        widget.setLayout(layout)
        return widget
    
    def create_calls_tab(self):
        """Create calls tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Filter controls
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Filter Group:"))
        self.calls_group_filter = QComboBox()
        self.calls_group_filter.setEditable(True)
        self.calls_group_filter.addItem("All")
        self.calls_group_filter.currentTextChanged.connect(self.apply_calls_filter)
        filter_layout.addWidget(self.calls_group_filter)
        
        filter_layout.addWidget(QLabel("Filter Client:"))
        self.calls_client_filter = QComboBox()
        self.calls_client_filter.setEditable(True)
        self.calls_client_filter.addItem("All")
        self.calls_client_filter.currentTextChanged.connect(self.apply_calls_filter)
        filter_layout.addWidget(self.calls_client_filter)
        
        filter_layout.addStretch()
        layout.addLayout(filter_layout)
        
        # Table
        self.calls_table = QTableWidget()
        self.calls_table.setColumnCount(10)
        self.calls_table.setHorizontalHeaderLabels([
            "⏱ Time", "📡 MCarrier", "📻 Carrier", "🎰 Slot", "🆔 CallID", "⭐ Pri", "📋 Type", "👤 From", "👥 To", "🔒 Mode"
        ])
        self.calls_table.setAlternatingRowColors(True)
        self.calls_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.calls_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.calls_table.verticalHeader().setVisible(False)
        self.calls_table.verticalHeader().setDefaultSectionSize(24)
        self.calls_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.calls_table.setWordWrap(False)
        self.calls_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        calls_header = self.calls_table.horizontalHeader()
        for col in range(10):
            calls_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        calls_header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        for col, w in enumerate([70, 80, 70, 50, 70, 50, 80, 90, 90, 100]):
            self.calls_table.setColumnWidth(col, w)
        
        layout.addWidget(self.calls_table)
        widget.setLayout(layout)
        return widget

    def create_groups_tab(self):
        """Create groups tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Table
        self.groups_table = QTableWidget()
        self.groups_table.setColumnCount(7)  # Back to 7 columns
        self.groups_table.setHorizontalHeaderLabels([
            "🆔 GSSI", "⏱ Last Seen", "🔴 REC", "🌍 MCC", "📍 MNC", "⭐ Priority", "📛 Name/Country"
        ])
        self.groups_table.setAlternatingRowColors(True)
        self.groups_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.groups_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.groups_table.verticalHeader().setVisible(False)
        self.groups_table.verticalHeader().setDefaultSectionSize(24)
        self.groups_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.groups_table.setWordWrap(False)
        self.groups_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        groups_header = self.groups_table.horizontalHeader()
        for col in range(6):
            groups_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        groups_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for col, w in enumerate([90, 90, 50, 60, 60, 70, 200]):
            self.groups_table.setColumnWidth(col, w)
        
        layout.addWidget(self.groups_table)
        widget.setLayout(layout)
        return widget

    def create_users_tab(self):
        """Create users tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Filter controls
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Filter Group:"))
        self.users_group_filter = QComboBox()
        self.users_group_filter.setEditable(True)
        self.users_group_filter.addItem("All")
        self.users_group_filter.currentTextChanged.connect(self.apply_users_filter)
        filter_layout.addWidget(self.users_group_filter)
        
        filter_layout.addStretch()
        layout.addLayout(filter_layout)
        
        # Table
        self.users_table = QTableWidget()
        self.users_table.setColumnCount(7)  # Back to 7 columns
        self.users_table.setHorizontalHeaderLabels([
            "🆔 ISSI", "⏱ Last Seen", "👥 GSSI", "🌍 MCC", "📍 MNC", "📛 Name", "📌 Location/Country"
        ])
        self.users_table.setAlternatingRowColors(True)
        self.users_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.users_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.users_table.verticalHeader().setVisible(False)
        self.users_table.verticalHeader().setDefaultSectionSize(24)
        self.users_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.users_table.setWordWrap(False)
        self.users_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        users_header = self.users_table.horizontalHeader()
        for col in range(6):
            users_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        users_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for col, w in enumerate([90, 90, 90, 60, 60, 120, 200]):
            self.users_table.setColumnWidth(col, w)
        
        layout.addWidget(self.users_table)
        widget.setLayout(layout)
        return widget
    
    def create_stats_tab(self):
        """Create statistics tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        self.stats_text = QTextEdit()
        self.stats_text.setReadOnly(True)
        self.stats_text.setFont(QFont("Consolas", 10))
        layout.addWidget(self.stats_text)
        
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.update_stats)
        layout.addWidget(refresh_btn)

        # Auto-refresh timer
        self.stats_timer = QTimer()
        self.stats_timer.timeout.connect(self.update_stats)
        self.stats_timer.start(1000)  # 1 second

        widget.setLayout(layout)
        return widget
    
    def apply_modern_style(self):
        """Apply shadcn/ui dark theme."""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0a0a0f; /* shadcn dark background */
            }
            QWidget {
                background-color: #0a0a0f;
                color: #fafafa; /* shadcn light text */
                font-family: 'Segoe UI', 'Inter', sans-serif;
                font-size: 13px;
            }
            
            /* Panels / Cards */
            QGroupBox {
                border: 1px solid #2a2a3a; /* shadcn border */
                border-radius: 8px;
                margin-top: 8px;
                padding: 12px;
                background-color: #1a1a24; /* shadcn card */
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #a1a1aa; /* shadcn muted text */
                font-weight: 600;
                font-size: 11px;
            }
            
            /* Buttons */
            QPushButton {
                background-color: #1a1a24; /* shadcn card */
                color: #fafafa;
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2a2a3a;
                border-color: #3a3a4a;
            }
            QPushButton:pressed {
                background-color: #3a3a4a;
            }
            QPushButton:disabled {
                background-color: #0a0a0f;
                color: #52525b;
                border-color: #1a1a24;
            }
            
            /* Primary Action Button (Start) */
            QPushButton#start_btn {
                background-color: #22c55e; /* green-500 */
                border-color: #16a34a;
                color: white;
            }
            QPushButton#start_btn:hover {
                background-color: #16a34a; /* green-600 */
            }
            
            /* Destructive Action Button (Stop) */
            QPushButton#stop_btn {
                background-color: #ef4444; /* red-500 */
                border-color: #dc2626;
                color: white;
            }
            QPushButton#stop_btn:hover {
                background-color: #dc2626; /* red-600 */
            }
            
            /* Inputs */
            QLineEdit, QComboBox {
                background-color: #1a1a24;
                color: #fafafa;
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                padding: 6px 10px;
                selection-background-color: #3b82f6;
                selection-color: white;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #3b82f6; /* shadcn accent */
            }
            
            /* Table */
            QTableWidget {
                background-color: #1a1a24;
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                gridline-color: #2a2a3a;
                alternate-background-color: #0f0f1a;
            }
            QTableWidget::item {
                padding: 6px 8px;
                border: none;
            }
            QTableWidget::item:selected {
                background-color: #2a2a3a;
                color: #fafafa;
            }
            QTableWidget::item:hover {
                background-color: #2a2a3a;
            }
            QHeaderView::section {
                background-color: #0f0f1a;
                color: #a1a1aa;
                padding: 8px 6px;
                border: none;
                border-bottom: 1px solid #2a2a3a;
                border-right: 1px solid #2a2a3a;
                font-weight: 600;
                font-size: 11px;
            }
            QHeaderView::section:hover {
                background-color: #1a1a24;
            }
            
            /* Tabs */
            QTabWidget::pane {
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                top: -1px;
                background-color: #1a1a24;
            }
            QTabBar::tab {
                background-color: #0a0a0f;
                color: #71717a;
                padding: 8px 16px;
                border: 1px solid transparent;
                border-bottom: 2px solid transparent;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                color: #fafafa;
                background-color: #1a1a24;
                border-bottom: 2px solid #3b82f6;
            }
            QTabBar::tab:hover {
                color: #e4e4e7;
            }
            
            /* Scrollbars */
            QScrollBar:vertical {
                border: none;
                background: #0a0a0f;
                width: 12px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #2a2a3a;
                min-height: 20px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover {
                background: #3a3a4a;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                border: none;
                background: #0a0a0f;
                height: 12px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #2a2a3a;
                min-width: 20px;
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #3a3a4a;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            
            /* Status Bar */
            QStatusBar {
                background-color: #1a1a24;
                color: #a1a1aa;
                border-top: 1px solid #2a2a3a;
            }
            
            /* Sliders */
            QSlider::groove:horizontal {
                border: none;
                height: 6px;
                background: #2a2a3a;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #fafafa;
                border: 2px solid #3b82f6;
                width: 18px;
                height: 18px;
                margin: -6px 0;
                border-radius: 9px;
            }
            QSlider::handle:horizontal:hover {
                background: #e4e4e7;
                border: 2px solid #2563eb;
            }
            QSlider::handle:horizontal:pressed {
                background: #a1a1aa;
            }
            QSlider::sub-page:horizontal {
                background: #3b82f6; /* shadcn accent */
                border-radius: 3px;
            }
            QSlider::add-page:horizontal {
                background: #2a2a3a;
                border-radius: 3px;
            }
            
            /* Checkbox */
            QCheckBox {
                color: #fafafa;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #2a2a3a;
                border-radius: 4px;
                background: #1a1a24;
            }
            QCheckBox::indicator:hover {
                border-color: #3a3a4a;
            }
            QCheckBox::indicator:checked {
                background: #3b82f6;
                border-color: #3b82f6;
            }
            QCheckBox::indicator:checked:hover {
                background: #2563eb;
                border-color: #2563eb;
            }
            
            /* Labels */
            QLabel {
                color: #fafafa;
            }
            
            /* Text Edit */
            QTextEdit {
                background-color: #1a1a24;
                color: #fafafa;
                border: 1px solid #2a2a3a;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        
        self.start_btn.setObjectName("start_btn")
        self.stop_btn.setObjectName("stop_btn")
    
    def on_monitor_raw_toggled(self, checked):
        """Handle raw monitor toggle."""
        try:
            self.settings_manager.set("monitor_raw", bool(checked))
            self.settings_manager.save()
        except Exception:
            pass
        if self.capture_thread:
            self.capture_thread.set_monitor_raw(checked)
        if checked:
            try:
                self._start_raw_stream()
                if self.capture_thread:
                    self.capture_thread.set_raw_audio_sink(self.feed_raw_fm)
            except Exception as e:
                self.log(f"Raw FM stream error: {e}")
                audio_logger.error("Raw FM stream start failed: %s", e)
            self.log("Raw Signal FM: ON (flux continu)")
            audio_logger.info("Raw FM monitor enabled (continuous stream)")
        else:
            if self.capture_thread:
                self.capture_thread.set_raw_audio_sink(None)
            self._stop_raw_stream()
            self.log("Raw Signal FM: OFF")
            audio_logger.info("Raw FM monitor disabled")

    def on_auto_decrypt_toggled(self, checked):
        """Handle auto-decrypt toggle."""
        try:
            self.settings_manager.set("auto_decrypt", bool(checked))
            self.settings_manager.save()
        except Exception as exc:
            logger.debug("Failed to persist auto-decrypt setting: %s", exc)
        if self.capture_thread:
            self.capture_thread.auto_decrypt = bool(checked)
            if getattr(self.capture_thread, "decoder", None):
                self.capture_thread.decoder.auto_decrypt = bool(checked)

    def on_preset_changed(self, text):
        """Handle frequency preset."""
        # Extract frequency from text "390.000 MHz - Label"
        try:
            if "MHz" in text:
                freq_str = text.split("MHz")[0].strip()
                self.freq_input.setText(freq_str)
        except:
            pass
    
    def on_bandwidth_changed(self, text):
        """Handle bandwidth change."""
        try:
            bw = float(text)
            if hasattr(self, 'waterfall'):
                self.waterfall.set_bandwidth(bw)
        except ValueError:
            pass

    def toggle_recording(self):
        """Toggle recording state."""
        self.recording_enabled = self.record_btn.isChecked()
        if not self.recording_enabled and self.recording_active:
            self.save_recording()
            
    def reset_stats(self):
        """Reset all statistics and tables."""
        self.frame_count = 0
        self.decrypted_count = 0
        self.tetra_sync_count = 0
        self.tetra_frame_count = 0
        self.tetra_valid_frames = 0
        self.signal_present = False
        self.first_frame_time = None
        self.cell_mcc = None
        self.cell_mnc = None
        self.cell_colour = None
        self.cell_la = None
        self.frames_table.setRowCount(0)
        self.calls_table.setRowCount(0)
        self.groups_table.setRowCount(0)
        self.users_table.setRowCount(0)
        self.update_displays()

    def on_tune(self):
        """Tune to frequency."""
        try:
            freq_mhz = float(self.freq_input.text())
            freq_hz = freq_mhz * 1e6
            
            # Reset stats and tables on retune
            self.reset_stats()
            
            # Update tuned frequency display
            if hasattr(self, 'waterfall'):
                self.waterfall.set_tuned_frequency(freq_mhz)
                self.waterfall.update()
            
            if self.capture_thread and self.capture_thread.isRunning():
                self.capture_thread.set_frequency(freq_hz)
            else:
                self.log(f"Frequency set to {freq_mhz} MHz")
        except ValueError:
            QMessageBox.warning(self, "Error", "Invalid frequency")
    
    def on_gain_slider_changed(self, value):
        """Handle gain slider change."""
        gain_value = value / 2.0  # Convert 0-100 to 0-50 with 0.5 steps
        self.gain_label.setText(f"{gain_value:.1f} dB")
        
        if self.capture_thread and self.capture_thread.isRunning():
            try:
                self.capture_thread.set_gain(gain_value)
            except:
                pass
    
    def on_gain_changed(self, gain_text):
        """Change gain (legacy method for compatibility)."""
        if self.capture_thread and self.capture_thread.isRunning():
            try:
                gain = gain_text if gain_text == 'auto' else float(gain_text)
                self.capture_thread.set_gain(gain)
                # Update slider if it exists
                if hasattr(self, 'gain_slider'):
                    self.gain_slider.setValue(int(gain * 2))
            except:
                pass
    
    def on_noise_floor_changed(self, value):
        """Handle noise floor (bottom dB) slider change."""
        self.noise_floor_label.setText(f"{value} dB")
        # Update spectrum widget power min
        if hasattr(self, 'waterfall'):
            self.waterfall.power_min = value
            # Invalidate grid cache
            self.waterfall.grid_cache = None
            self.waterfall.grid_cache_params = None
            self.waterfall.update()
            
    def on_threshold_changed(self, value):
        """Handle threshold slider change."""
        self.threshold_label.setText(f"{value} dB")
        if hasattr(self, 'waterfall'):
            self.waterfall.set_noise_floor(value)
    
    def on_sample_rate_slider_changed(self, value):
        """Handle sample rate slider change."""
        # Get valid rate from slider index
        if hasattr(self, 'valid_sample_rates') and value < len(self.valid_sample_rates):
            sample_rate_mhz = self.valid_sample_rates[value]
        else:
            # Fallback to old calculation if valid_rates not set
            sample_rate_mhz = 1.8 + (value * 0.1)
        
        self.sample_rate_label.setText(f"{sample_rate_mhz:.3f} MHz")
        self.sample_rate = sample_rate_mhz * 1e6
        
        # Update capture thread if running
        if hasattr(self, 'capture_thread') and self.capture_thread and self.capture_thread.isRunning():
            self.capture_thread.set_sample_rate(self.sample_rate)
    
    def on_zoom_changed(self, value):
        """Handle zoom slider change."""
        zoom_level = value / 10.0
        if hasattr(self, 'waterfall'):
            self.waterfall.set_zoom(zoom_level)
            
    def on_range_changed(self, value):
        """Handle range (top dB) slider change."""
        self.range_label.setText(f"{value} dB")
        # Adjust top dB level
        if hasattr(self, 'waterfall'):
            self.waterfall.power_max = value
            self.waterfall.grid_cache = None
            self.waterfall.update()
            
    def on_denoiser_toggled(self, checked):
        """Handle denoiser toggle."""
        if hasattr(self, 'waterfall'):
            self.waterfall.set_denoiser(checked)
    
    def on_auto_spectrum_toggled(self, checked):
        """Handle auto spectrum slider toggle."""
        self.auto_spectrum_enabled = checked
        if checked and hasattr(self, 'waterfall') and self.waterfall.current_fft is not None:
            self._apply_auto_spectrum(self.waterfall.current_freqs, self.waterfall.current_fft)
    
    def _apply_auto_spectrum(self, freqs, powers):
        """Automatically adjust spectrum sliders and zoom."""
        if (not self.auto_spectrum_enabled) or powers is None:
            return
        
        import time
        now = time.time()
        if now - getattr(self, '_last_auto_spectrum_time', 0) < 0.3:
            return
        self._last_auto_spectrum_time = now
        
        power_array = np.asarray(powers, dtype=float)
        if power_array.size < 32:
            return
        
        finite_mask = np.isfinite(power_array)
        if not np.any(finite_mask):
            return
        
        usable = power_array[finite_mask]
        noise_floor = float(np.percentile(usable, 20))
        peak_power = float(np.percentile(usable, 99))
        
        if not np.isfinite(noise_floor) or not np.isfinite(peak_power):
            return
        
        dynamic_range = peak_power - noise_floor
        if dynamic_range < 5:
            return
        
        top_target = peak_power + 5.0
        bottom_target = noise_floor - 10.0
        if bottom_target > top_target - 10.0:
            bottom_target = top_target - 10.0
        threshold_target = noise_floor + dynamic_range * 0.6
        
        if hasattr(self, 'range_slider'):
            top_target = int(round(np.clip(top_target, self.range_slider.minimum(), self.range_slider.maximum())))
        else:
            top_target = int(round(top_target))
        
        if hasattr(self, 'noise_floor_slider'):
            bottom_target = int(round(np.clip(bottom_target, self.noise_floor_slider.minimum(), self.noise_floor_slider.maximum())))
        else:
            bottom_target = int(round(bottom_target))
        
        bottom_target = min(bottom_target, top_target - 5)
        
        if hasattr(self, 'threshold_slider'):
            threshold_target = int(round(np.clip(threshold_target, self.threshold_slider.minimum(), self.threshold_slider.maximum())))
        else:
            threshold_target = int(round(threshold_target))
        
        threshold_target = max(min(threshold_target, top_target - 2), bottom_target + 2)
        
        if hasattr(self, 'range_slider') and abs(self.range_slider.value() - top_target) >= 1:
            self.range_slider.setValue(top_target)
        if hasattr(self, 'noise_floor_slider') and abs(self.noise_floor_slider.value() - bottom_target) >= 1:
            self.noise_floor_slider.setValue(bottom_target)
        if hasattr(self, 'threshold_slider') and abs(self.threshold_slider.value() - threshold_target) >= 1:
            self.threshold_slider.setValue(threshold_target)
        
        if freqs is None or not hasattr(self, 'zoom_slider'):
            return
        
        freq_array = np.asarray(freqs, dtype=float)
        if freq_array.size != power_array.size or freq_array.size == 0:
            return
        
        # Convert to MHz if values look like Hz
        if np.max(np.abs(freq_array)) > 1e3:
            freq_mhz = freq_array / 1e6
        else:
            freq_mhz = freq_array
        
        full_span = float(np.max(freq_mhz) - np.min(freq_mhz))
        if full_span <= 0:
            return
        
        signal_mask = finite_mask & (power_array > noise_floor + 6)
        if np.any(signal_mask):
            active_freqs = freq_mhz[signal_mask]
            active_span = float(np.max(active_freqs) - np.min(active_freqs))
        else:
            active_span = full_span
        
        if active_span <= 0:
            active_span = full_span
        
        desired_span = min(full_span, max(active_span * 3, full_span / 10.0))
        desired_zoom = max(1.0, min(10.0, full_span / max(desired_span, 1e-9)))
        zoom_target = int(round(desired_zoom * 10))
        zoom_target = max(self.zoom_slider.minimum(), min(self.zoom_slider.maximum(), zoom_target))
        
        if abs(self.zoom_slider.value() - zoom_target) >= 1:
            self.zoom_slider.setValue(zoom_target)

    def on_tune_from_spectrum(self, freq_mhz):
        """Handle tuning from spectrum click."""
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            # Higher precision to keep the clicked spike centered.
            self.freq_input.setText(f"{freq_mhz:.6f}")
            if hasattr(self, "waterfall") and self.waterfall is not None:
                self.waterfall.center_view_on(freq_mhz)
        else:
            self.freq_input.setText(f"{freq_mhz:.3f}")
        self.on_tune()

    def on_start(self):
        """Start capture."""
        try:
            freq_mhz = float(self.freq_input.text())
            freq_hz = freq_mhz * 1e6
            
            # Get gain from slider
            if hasattr(self, 'gain_slider'):
                gain = self.gain_slider.value() / 2.0
            else:
                gain = 50.0  # Default per user specification
            
            # Get sample rate from slider (indexes valid RTL-SDR rates)
            if hasattr(self, 'sample_rate_slider') and hasattr(self, 'valid_sample_rates'):
                idx = int(self.sample_rate_slider.value())
                idx = max(0, min(idx, len(self.valid_sample_rates) - 1))
                sample_rate = float(self.valid_sample_rates[idx]) * 1e6
            else:
                sample_rate = 2.4e6  # Default per TETRA spec
            # Prefer 2.4 MHz for π/4-DQPSK resample to 18 ksym/s
            if abs(sample_rate - 2.4e6) > 1e3:
                logger.info(
                    "Sample rate %.3f MHz selected; 2.4 MHz recommended for TETRA decode",
                    sample_rate / 1e6,
                )
            
            self.capture_thread = CaptureThread()
            self.capture_thread.frequency = freq_hz
            self.capture_thread.gain = gain
            self.capture_thread.sample_rate = sample_rate
            self.capture_thread.sdr_device = self.settings_manager.get("sdr_device", None)
            self.capture_thread.auto_decrypt = self.auto_decrypt_cb.isChecked()
            self.capture_thread.set_monitor_raw(self.monitor_raw_cb.isChecked())
            if self.encryption_keys:
                self.capture_thread.set_keys(self.encryption_keys)
            
            self.capture_thread.signal_detected.connect(self.on_signal)
            self.capture_thread.signal_lost.connect(self.on_signal_lost)
            self.capture_thread.frame_decoded.connect(self.on_frame)
            self.capture_thread.error_occurred.connect(self.on_error)
            self.capture_thread.status_update.connect(self.on_status)
            self.capture_thread.spectrum_update.connect(self.on_spectrum)
            self.capture_thread.raw_audio_data.connect(self.on_raw_audio)
            self.capture_thread.voice_audio_data.connect(self.on_voice_audio)
            
            self.capture_thread.start()

            # If Raw FM already checked, attach direct sink now
            if self.monitor_raw_cb.isChecked():
                try:
                    self._start_raw_stream()
                    self.capture_thread.set_raw_audio_sink(self.feed_raw_fm)
                except Exception as e:
                    audio_logger.error("Raw FM autostart failed: %s", e)
            
            # Connect spectrum click signal
            self.waterfall.frequency_clicked.connect(self.on_tune_from_spectrum)

            # Lock listening-channel bars to the started frequency immediately
            if hasattr(self, "waterfall") and self.waterfall is not None:
                self.waterfall.set_tuned_frequency(freq_mhz)
            
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.log("Capture started")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
    
    def on_stop(self):
        """Stop capture."""
        # Save any active recording
        if self.recording_active:
            self.save_recording()
        
        if self.capture_thread:
            self.capture_thread.set_raw_audio_sink(None)
            self.capture_thread.stop()
            self.capture_thread.wait()
            self.capture_thread = None
        
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.log("Capture stopped")
    
    def on_scan(self):
        """Open scanner."""
        if not self.scanner_dialog:
            self.scanner_dialog = ScannerDialog(self)
            self.scanner_dialog.scan_complete.connect(self.on_scan_complete)
        self.scanner_dialog.show()
    
    def on_scan_complete(self, results):
        """Handle scan results."""
        if results and len(results) > 0:
            # Sort by power
            results.sort(key=lambda x: x.get('power_db', -100), reverse=True)
            best = results[0]
            best_freq = best['frequency']
            best_power = best['power_db']
            
            self.freq_input.setText(f"{best_freq/1e6:.3f}")
            self.log(f"Scan complete: Best signal at {best_freq/1e6:.3f} MHz ({best_power:.1f} dB)")
        else:
            self.log("Scan complete: No signals found")
    
    def on_load_keys(self):
        """Load encryption keys."""
        file_path, _ = QFileDialog.getOpenFileName(self, "Load Keys", "", "Key Files (*.txt);;All Files (*.*)")
        if file_path:
            try:
                # Load keys from file
                keys_loaded = []
                with open(file_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        # Skip comments and empty lines
                        if not line or line.startswith('#') or line.startswith('//'):
                            continue
                        
                        # Remove any non-hex characters
                        key = ''.join(c for c in line if c in '0123456789abcdefABCDEF')
                        
                        # Validate key length (80 bits = 20 hex chars, but accept 16-32 chars)
                        if 16 <= len(key) <= 32:
                            keys_loaded.append(key.lower())
                
                if keys_loaded:
                    # Pass keys to capture thread if running
                    if self.capture_thread and self.capture_thread.isRunning():
                        self.capture_thread.set_keys(keys_loaded)
                    
                    # Store keys for future captures
                    self.encryption_keys = keys_loaded
                    
                    self.log(f"✅ Loaded {len(keys_loaded)} encryption keys from {file_path}")
                    self.log(f"🔑 Auto-decrypt will try these keys on encrypted frames")
                else:
                    self.log(f"⚠️ No valid keys found in {file_path}")
            except Exception as e:
                self.log(f"❌ Error loading keys: {e}")
                logger.error(f"Error loading keys: {e}")

    
    @pyqtSlot(float, float)
    def on_signal(self, freq, power):
        """Handle signal detection."""
        self.signal_present = True
        self.signal_label.setText(f"🟢 {power:.1f} dB")
        
        # If we detect a signal but haven't decoded TETRA frames yet, show status
        # Only show this if we actually have a signal (not noise)
        if self.tetra_frame_count == 0:
            self.tetra_status_label.setText("🟡 Signal Detected (Decoding...)")
            self.tetra_status_label.setStyleSheet(
                "font-weight: bold; padding: 5px; color: #ffaa00; background-color: #221100;"
            )

    def on_signal_lost(self):
        """Handle signal loss."""
        self.signal_present = False
        self.signal_label.setText("🔴 No Signal")
        # Clear TETRA status when signal is lost - they should be mutually exclusive
        self.tetra_status_label.setText("⚫ No TETRA Signal")
        self.tetra_status_label.setStyleSheet(
            "font-weight: bold; padding: 5px; color: #888888;"
        )
    
    def _on_monitor_audio_toggled(self, checked: bool):
        """Persist Monitor Audio checkbox and (re)start the player thread."""
        self.settings_manager.set("monitor_audio", bool(checked), persist=True)
        if checked:
            self._voice_monitor_hint_shown = False
            self.init_audio()
            self.log("Monitor Audio activé")
        else:
            self._stop_audio_player()
            self.log("Monitor Audio désactivé — la voix décodée ne sera pas jouée")

    def _resolve_output_device(self):
        """Pick a concrete output device index (prefer settings, else pulse/default)."""
        device = self.settings_manager.get("audio_device", None)
        if device is not None:
            return device
        try:
            devices = list(sd.query_devices())
            # Prefer named hosts that usually reach the desktop speakers
            for prefer in ("pulse", "default", "pipewire"):
                for i, info in enumerate(devices):
                    if info.get("max_output_channels", 0) <= 0:
                        continue
                    if prefer in str(info.get("name", "")).lower():
                        return i
            defaults = sd.default.device
            if isinstance(defaults, (list, tuple)) and len(defaults) >= 2 and defaults[1] is not None:
                if int(defaults[1]) >= 0:
                    return int(defaults[1])
            for i, info in enumerate(devices):
                if info.get("max_output_channels", 0) > 0:
                    return i
        except Exception:
            pass
        return None

    def _test_speaker(self):
        """Play a short beep via the audio queue (never call sd.play from the UI thread)."""
        try:
            if self._audio_player_thread is None or not self._audio_player_thread.is_alive():
                self.init_audio()
            # Generate at 8 kHz — player resamples. Avoid concurrent sd.play (SIGABRT).
            sr = 8000
            t = np.linspace(0, 0.35, int(sr * 0.35), endpoint=False)
            beep = (0.35 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
            n = min(len(beep), int(0.02 * sr))
            if n > 0:
                beep[:n] *= np.linspace(0, 1, n)
                beep[-n:] *= np.linspace(1, 0, n)
            self._enqueue_audio(beep)
            device = self._resolve_output_device()
            self.log(f"Test Speaker — bip 880 Hz (device={device})")
            audio_logger.info("Test speaker beep queued (device=%s)", device)
        except Exception as e:
            self.log(f"Test Speaker échoué: {e}")
            audio_logger.error("Test speaker failed: %s", e)

    def _stop_audio_player(self):
        """Stop background playback thread."""
        self._stop_raw_stream()
        self._audio_player_stop.set()
        try:
            self._audio_q.put_nowait(None)  # wake worker
        except Exception:
            pass
        thread = self._audio_player_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._audio_player_thread = None

    def _raw_ring_clear(self):
        with self._raw_ring_lock:
            self._raw_ring_r = 0
            self._raw_ring_w = 0
            self._raw_ring_size = 0

    def _raw_ring_write(self, samples: np.ndarray) -> None:
        """Append samples to the raw ring; drop oldest on overflow (keeps realtime)."""
        if samples is None or samples.size == 0 or self._raw_ring_data is None:
            return
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        with self._raw_ring_lock:
            cap = self._raw_ring_cap
            n = int(x.size)
            if n >= cap:
                # Keep only the newest window
                x = x[-cap:]
                n = cap
                self._raw_ring_data[:] = x
                self._raw_ring_r = 0
                self._raw_ring_w = 0
                self._raw_ring_size = n
                self._raw_drops += 1
                return
            free = cap - self._raw_ring_size
            if n > free:
                drop = n - free
                self._raw_ring_r = (self._raw_ring_r + drop) % cap
                self._raw_ring_size -= drop
                self._raw_drops += 1
            # Contiguous write, possibly wrapped
            first = min(n, cap - self._raw_ring_w)
            self._raw_ring_data[self._raw_ring_w : self._raw_ring_w + first] = x[:first]
            second = n - first
            if second:
                self._raw_ring_data[:second] = x[first:]
            self._raw_ring_w = (self._raw_ring_w + n) % cap
            self._raw_ring_size += n

    def _raw_ring_read(self, n: int) -> np.ndarray:
        """Read n samples; pad with zeros on underrun."""
        out = np.zeros(n, dtype=np.float32)
        if self._raw_ring_data is None or n <= 0:
            return out
        with self._raw_ring_lock:
            have = min(n, self._raw_ring_size)
            if have <= 0:
                self._raw_underruns += 1
                return out
            cap = self._raw_ring_cap
            first = min(have, cap - self._raw_ring_r)
            out[:first] = self._raw_ring_data[self._raw_ring_r : self._raw_ring_r + first]
            second = have - first
            if second:
                out[first:have] = self._raw_ring_data[:second]
            self._raw_ring_r = (self._raw_ring_r + have) % cap
            self._raw_ring_size -= have
            if have < n:
                self._raw_underruns += 1
        return out

    def _start_raw_stream(self):
        """Start a continuous PortAudio OutputStream for Raw FM (no blocking bursts)."""
        self._stop_raw_stream()
        device = self._resolve_output_device()
        out_rate = 48000
        try:
            if device is not None:
                info = sd.query_devices(device, "output")
                native = int(info.get("default_samplerate") or 48000)
                if native > 0:
                    out_rate = native
        except Exception:
            pass
        self._audio_out_rate = out_rate
        # ~1.5 s ring at device rate — absorbs capture jitter without huge latency
        cap = max(out_rate * 2, 96000)
        self._raw_ring_cap = cap
        self._raw_ring_data = np.zeros(cap, dtype=np.float32)
        self._raw_ring_clear()
        self._raw_underruns = 0
        self._raw_drops = 0

        def _callback(outdata, frames, time_info, status):  # noqa: ARG001
            if status:
                audio_logger.debug("Raw stream status: %s", status)
            samples = self._raw_ring_read(frames)
            outdata[:, 0] = samples

        stream = sd.OutputStream(
            samplerate=out_rate,
            channels=1,
            dtype="float32",
            device=device,
            blocksize=0,  # let PortAudio choose (low latency)
            latency="low",
            callback=_callback,
        )
        stream.start()
        self._raw_stream = stream
        audio_logger.info(
            "Raw FM continuous stream started (device=%s, rate=%s, ring=%ss)",
            device,
            out_rate,
            cap / out_rate,
        )

    def _stop_raw_stream(self):
        stream = getattr(self, "_raw_stream", None)
        self._raw_stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self._raw_ring_clear()
        if getattr(self, "_raw_underruns", 0) or getattr(self, "_raw_drops", 0):
            audio_logger.info(
                "Raw FM stream stopped (underruns=%s drops=%s)",
                getattr(self, "_raw_underruns", 0),
                getattr(self, "_raw_drops", 0),
            )

    def _append_monitor_wav(self, samples_8k: np.ndarray):
        """Append decoded 8 kHz float audio to a rolling monitor WAV for offline check.

        Inserts silence for wall-clock gaps so dropped/rejected frames do not
        glue into a chopped stutter — the timeline stays continuous.
        """
        try:
            import wave
            import time as _time
            from datetime import datetime

            if samples_8k is None or len(samples_8k) == 0:
                return
            now = _time.monotonic()
            if not getattr(self, "_monitor_wav", None):
                records_dir = str(RECORDS_DIR)
                os.makedirs(records_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = os.path.join(records_dir, f"tetra_monitor_{ts}.wav")
                self._monitor_wav_path = path
                self._monitor_wav = wave.open(path, "wb")
                self._monitor_wav.setnchannels(1)
                self._monitor_wav.setsampwidth(2)
                self._monitor_wav.setframerate(8000)
                self._monitor_wav_last_t = now
                audio_logger.info("Monitor WAV: %s", path)
                self.log(f"Enregistrement moniteur: {os.path.basename(path)}")
            else:
                last_t = getattr(self, "_monitor_wav_last_t", None)
                if last_t is not None:
                    gap = now - last_t
                    # Expected ~60 ms per TCH slot pair; fill only real gaps
                    if 0.08 < gap < 2.5:
                        n_sil = int(round(gap * 8000))
                        if n_sil > 0:
                            self._monitor_wav.writeframes(
                                np.zeros(n_sil, dtype=np.int16).tobytes()
                            )
            pcm = (np.clip(samples_8k, -1.0, 1.0) * 32767).astype(np.int16)
            self._monitor_wav.writeframes(pcm.tobytes())
            self._monitor_wav_last_t = now + (len(samples_8k) / 8000.0)
        except Exception as e:
            audio_logger.debug("Monitor WAV write failed: %s", e)

    @staticmethod
    def _unpack_audio_chunk(chunk):
        """Normalize queue items to (float32 samples, input_rate_hz)."""
        if chunk is None:
            return None, 8000
        if isinstance(chunk, tuple) and len(chunk) == 2:
            samples, rate = chunk
            try:
                rate = int(rate) if rate else 8000
            except (TypeError, ValueError):
                rate = 8000
            return np.asarray(samples, dtype=np.float32).reshape(-1), max(1000, rate)
        return np.asarray(chunk, dtype=np.float32).reshape(-1), 8000

    def _audio_player_loop(self, device, out_rate: int):
        """Drain queued float chunks (any rate), coalesce, play via PortAudio."""
        import time as _time

        audio_logger.info(
            "Audio player thread running (device=%s, out_rate=%s)", device, out_rate
        )
        while not self._audio_player_stop.is_set():
            try:
                chunk = self._audio_q.get(timeout=0.25)
            except queue.Empty:
                continue
            if chunk is None:
                if self._audio_player_stop.is_set():
                    break
                continue
            try:
                first, in_rate = self._unpack_audio_chunk(chunk)
                if first is None or first.size == 0:
                    continue
                parts = [first]
                # Voice (8 kHz): longer coalesce. Raw FM (48 kHz): short, or the
                # queue floods and playback falls silently behind.
                if in_rate >= 16000:
                    deadline = _time.monotonic() + 0.08
                    extend = 0.04
                    get_to = 0.04
                else:
                    deadline = _time.monotonic() + 0.45
                    extend = 0.18
                    get_to = 0.10
                while _time.monotonic() < deadline and not self._audio_player_stop.is_set():
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        nxt = self._audio_q.get(timeout=min(get_to, remaining))
                    except queue.Empty:
                        break
                    if nxt is None:
                        if self._audio_player_stop.is_set():
                            break
                        continue
                    nxt_s, nxt_rate = self._unpack_audio_chunk(nxt)
                    if nxt_s is None or nxt_s.size == 0:
                        continue
                    if nxt_rate != in_rate:
                        # Different stream (e.g. voice vs raw) — put back and flush current
                        try:
                            self._audio_q.put_nowait((nxt_s, nxt_rate))
                        except queue.Full:
                            pass
                        break
                    parts.append(nxt_s)
                    deadline = _time.monotonic() + extend

                samples = np.concatenate(parts) if len(parts) > 1 else parts[0]
                if samples.size == 0:
                    continue
                peak = float(np.max(np.abs(samples)))
                if peak > 0 and peak < 0.25:
                    samples = np.clip(samples * (0.5 / peak), -1.0, 1.0)
                    peak = float(np.max(np.abs(samples)))

                # Monitor WAV is 8 kHz voice only — never dump raw FM into it
                if in_rate == 8000:
                    self._append_monitor_wav(samples)

                play = samples
                if out_rate != in_rate:
                    n_out = max(1, int(round(samples.size * (out_rate / float(in_rate)))))
                    play = resample(samples, n_out).astype(np.float32)

                with self._audio_play_lock:
                    sd.play(play, samplerate=out_rate, device=device, blocking=True)
                audio_logger.debug(
                    "Played audio: %s chunks, %s samples in=%sHz out=%sHz (peak=%.3f, device=%s)",
                    len(parts),
                    play.size,
                    in_rate,
                    out_rate,
                    peak,
                    device,
                )
            except Exception as exc:
                audio_logger.warning("Audio play failed: %s", exc)
                try:
                    self._audio_player_stop.wait(0.2)
                except Exception:
                    pass
        audio_logger.info("Audio player thread stopped")

    def _enqueue_audio(self, data: np.ndarray, sample_rate: int = 8000):
        """Queue float32 mono samples with their true sample rate for the player."""
        if data is None or len(data) == 0:
            return
        play = np.clip(np.asarray(data, dtype=np.float32).reshape(-1), -1.0, 1.0)
        item = (play.copy(), int(sample_rate) if sample_rate else 8000)
        try:
            self._audio_q.put_nowait(item)
        except queue.Full:
            try:
                _ = self._audio_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio_q.put_nowait(item)
            except queue.Full:
                audio_logger.debug("Audio queue full — dropping chunk (rate=%s)", sample_rate)

    def init_audio(self):
        """Start background voice player (8 kHz in → device rate out)."""
        try:
            self._stop_audio_player()
            # Fresh queue after stop
            self._audio_q = queue.Queue(maxsize=64)
            self._audio_player_stop = threading.Event()

            device = self._resolve_output_device()
            out_rate = 48000
            try:
                if device is not None:
                    info = sd.query_devices(device, "output")
                    native = int(info.get("default_samplerate") or 48000)
                    if native > 0:
                        out_rate = native
            except Exception:
                pass
            self._audio_out_rate = out_rate

            self._audio_player_thread = threading.Thread(
                target=self._audio_player_loop,
                args=(device, out_rate),
                name="TetraEarAudio",
                daemon=True,
            )
            self._audio_player_thread.start()
            audio_logger.info(
                "Audio player started (device=%s, out_rate=%s)", device, out_rate
            )
            self.log(f"Audio OK — sortie {out_rate} Hz (device={device})")
        except Exception as e:
            self.log(f"Audio init error: {e}")
            audio_logger.error("Audio init error: %s", e)

    def feed_raw_fm(self, data: np.ndarray):
        """Thread-safe Raw FM → ring (callable from the capture thread)."""
        if data is None or len(data) == 0:
            return
        if not getattr(self, "monitor_raw_cb", None) or not self.monitor_raw_cb.isChecked():
            return
        try:
            if self._raw_stream is None:
                # Stream must be started on GUI side; ignore until then
                return
            x = np.asarray(data, dtype=np.float32).reshape(-1)
            in_rate = 48000.0
            out_rate = float(getattr(self, "_audio_out_rate", 48000) or 48000)
            if abs(out_rate - in_rate) > 1.0 and x.size > 1:
                n_out = max(1, int(round(x.size * (out_rate / in_rate))))
                # np.interp is fine here; avoid allocating linspace twice via arange
                x = np.interp(
                    np.linspace(0, x.size - 1, n_out, dtype=np.float64),
                    np.arange(x.size, dtype=np.float64),
                    x,
                ).astype(np.float32)
            self._raw_ring_write(x)
            n = getattr(self, "_raw_audio_play_count", 0) + 1
            self._raw_audio_play_count = n
            if n <= 3 or n % 500 == 0:
                with self._raw_ring_lock:
                    fill = self._raw_ring_size
                    cap = self._raw_ring_cap
                audio_logger.info(
                    "Raw FM ring write: %s samples (peak=%.3f, fill=%.0f%%, underrun=%s drop=%s)",
                    x.size,
                    float(np.max(np.abs(x))) if x.size else 0.0,
                    100.0 * fill / max(cap, 1),
                    self._raw_underruns,
                    self._raw_drops,
                )
        except Exception as e:
            audio_logger.warning("Raw FM ring write failed: %s", e)

    @pyqtSlot(np.ndarray)
    def on_raw_audio(self, data):
        """Fallback path if no direct sink is attached."""
        self.feed_raw_fm(data)

    @pyqtSlot(np.ndarray)
    def on_voice_audio(self, data):
        """Play decoded TETRA voice data live and record to continuous WAV."""
        import os
        from datetime import datetime
        import wave
        
        # Validate amplitude (must be > 0)
        if len(data) == 0:
            return
            
        max_amp = float(np.max(np.abs(data)))
        if max_amp <= 0:
            return  # Skip silent/invalid frames
        
        # Continuous recording to single WAV file
        try:
            # Convert float32 to int16
            audio_int16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
            
            # Start new recording if not active
            if self.recording_enabled and not self.recording_active:
                records_dir = str(RECORDS_DIR)
                os.makedirs(records_dir, exist_ok=True)
                
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                freq_mhz = float(self.freq_input.text())
                self.current_wav_file = os.path.join(records_dir, f"tetra_voice_{freq_mhz:.3f}MHz_{timestamp}.wav")
                
                # Open WAV file for writing
                self.wav_file = wave.open(self.current_wav_file, 'wb')
                self.wav_file.setnchannels(1)
                self.wav_file.setsampwidth(2)
                self.wav_file.setframerate(8000)
                
                self.recording_active = True
                self.recording_start_time = datetime.now()
                self.recording_has_audio = False
                self.log(f"🎙️ Recording: {os.path.basename(self.current_wav_file)}")
                audio_logger.info("Started voice recording: %s", self.current_wav_file)
            
            # Write to continuous WAV file
            if self.recording_active and hasattr(self, 'wav_file'):
                self.wav_file.writeframes(audio_int16.tobytes())
                self.recording_has_audio = True
            
        except Exception as e:
            audio_logger.debug("Failed to record voice: %s", e)
        
        # Play audio LIVE if monitoring enabled
        if not self.hear_voice_cb.isChecked():
            if not getattr(self, "_voice_monitor_hint_shown", False):
                self._voice_monitor_hint_shown = True
                self.log("Voix décodée reçue — coche « Monitor Audio » pour l'écouter")
                audio_logger.info("Decoded voice arrived but Monitor Audio is off")
            return

        # Raw FM uses an exclusive continuous OutputStream — don't fight it with
        # bursty voice playback (that was a major source of choppy FM).
        if self.monitor_raw_cb.isChecked() and self._raw_stream is not None:
            return

        try:
            if self._audio_player_thread is None or not self._audio_player_thread.is_alive():
                self.init_audio()
            self._enqueue_audio(data)
            audio_logger.info(
                "Queued voice for playback: %s samples (max_amp=%.4f, q=%s)",
                len(data),
                max_amp,
                self._audio_q.qsize(),
            )
        except Exception as e:
            audio_logger.debug("Audio enqueue error: %s", e)
            try:
                self.init_audio()
                self._enqueue_audio(data)
            except Exception as e2:
                audio_logger.debug("Audio reinit failed: %s", e2)

    def save_recording(self):
        """Close current recording file."""
        if self.recording_active and hasattr(self, 'wav_file'):
            try:
                self.wav_file.close()
                duration = (datetime.now() - self.recording_start_time).total_seconds()
                
                # Check if we should keep the file
                save_silence = self.settings_manager.get("save_silence", False)
                if not save_silence and not self.recording_has_audio:
                    # Delete silent file
                    try:
                        os.remove(self.current_wav_file)
                        self.log(f"Deleted silent recording: {os.path.basename(self.current_wav_file)}")
                        audio_logger.info("Deleted silent voice recording: %s", self.current_wav_file)
                    except Exception as e:
                        audio_logger.debug("Failed to delete silent file: %s", e)
                else:
                    self.log(f"Saved voice recording: {os.path.basename(self.current_wav_file)} ({duration:.1f}s)")
                    audio_logger.info("Saved voice recording: %s (%.1fs)", self.current_wav_file, duration)

                    # Optional MP3 export for easier sharing (requires ffmpeg).
                    if self.settings_manager.get("export_mp3", False):
                        try:
                            from tetraear.audio.export import wav_to_mp3

                            mp3_path = wav_to_mp3(self.current_wav_file)
                            self.log(f"Exported MP3: {os.path.basename(str(mp3_path))}")
                            audio_logger.info("Exported MP3: %s", mp3_path)
                        except FileNotFoundError:
                            self.log("MP3 export skipped (ffmpeg not found)")
                            audio_logger.info("MP3 export skipped (ffmpeg not found)")
                        except Exception as e:
                            self.log(f"MP3 export failed: {e}")
                            audio_logger.debug("MP3 export failed: %s", e)
                
                self.recording_active = False
            except Exception as e:
                audio_logger.debug("Failed to close recording: %s", e)
    
    def reassemble_sds_message(self, frame):
        """Reassemble SDS fragments into complete message or parse existing."""
        # First check if the frame already has an SDS message parsed by the decoder
        if 'sds_message' in frame and frame['sds_message']:
            # Already parsed, just return it
            return frame['sds_message']
        
        if 'decoded_text' in frame and frame['decoded_text']:
            # Already has decoded text
            return frame['decoded_text']

        # Don't attempt to parse SDS from encrypted frames unless they were decrypted.
        if frame.get("encrypted") and not frame.get("decrypted"):
            return None

        # Only attempt single-frame SDS parsing on SDS-candidate PDUs.
        mac_pdu = frame.get("mac_pdu") if isinstance(frame.get("mac_pdu"), dict) else {}
        pdu_type = str(mac_pdu.get("type", "")).upper()
        type_name = str(frame.get("type_name", "")).upper()
        is_sds_pdu = pdu_type in ("MAC_DATA", "MAC_SUPPL") or ("MAC-DATA" in type_name) or ("MAC-SUPPL" in type_name) or ("MAC-END" in type_name) or ("MAC-END/RES" in type_name)
        
        # Multi-frame SDS reassembly logic
        # Check if this is a fragmented SDS message
        addr_src = frame.get('address_source', None)
        addr_dst = frame.get('address_destination', None)
        frag_id = frame.get('fragment_id', None)
        is_last = frame.get('is_last_fragment', False)
        
        # If we have fragment info, handle reassembly
        if addr_src and frag_id is not None:
            key = f"{addr_src}_{addr_dst}_{frag_id}"
            
            if not hasattr(self, 'sds_fragments'):
                self.sds_fragments = {}
            
            # Add fragment to buffer
            if key not in self.sds_fragments:
                self.sds_fragments[key] = []
            
            self.sds_fragments[key].append(frame)
            
            # If this is the last fragment, reassemble
            if is_last and len(self.sds_fragments[key]) > 1:
                fragments = sorted(self.sds_fragments[key], key=lambda f: f.get('fragment_index', 0))
                
                # Concatenate data from all fragments
                combined_data = b''
                for frag in fragments:
                    if 'decrypted_bytes' in frag:
                        try:
                            combined_data += bytes.fromhex(frag['decrypted_bytes'])
                        except:
                            pass
                    elif 'mac_pdu' in frag and 'data' in frag['mac_pdu']:
                        data = frag['mac_pdu']['data']
                        if isinstance(data, bytes):
                            combined_data += data
                        elif isinstance(data, str):
                            try:
                                combined_data += bytes.fromhex(data.replace(' ', ''))
                            except:
                                pass
                
                # Parse the complete message
                if combined_data:
                    from tetraear.core.protocol import TetraProtocolParser
                    parser = TetraProtocolParser()
                    sds_text = parser.parse_sds_data(combined_data)
                    
                    if sds_text:
                        # Mark all fragments as reassembled
                        for frag in fragments:
                            frag['is_reassembled'] = True
                            frag['sds_message'] = f"🧩 {sds_text}"
                        
                        # Clean up
                        del self.sds_fragments[key]
                        return f"🧩 {sds_text}"
        
        # Try to parse single-frame SDS from decrypted bytes or MAC data
        if not is_sds_pdu:
            return None

        data = None
        if 'decrypted_bytes' in frame:
            try:
                data = bytes.fromhex(frame['decrypted_bytes'])
            except:
                pass
        elif 'mac_pdu' in frame and 'data' in frame['mac_pdu']:
            data = frame['mac_pdu']['data']
            if isinstance(data, str):
                try:
                    data = bytes.fromhex(data.replace(' ', ''))
                except:
                    pass
        
        if not data or len(data) < 1:
            return None
        
        # Try to parse as SDS
        from tetraear.core.protocol import TetraProtocolParser
        parser = TetraProtocolParser()
        sds_text = parser.parse_sds_data(data)
        
        if sds_text:
            return sds_text
        
        # Try to decode as text if it looks printable - ONLY for non-encrypted frames
        # Check if frame is NOT encrypted
        is_encrypted = frame.get('encrypted', False)
        encryption_type = frame.get('encryption_type', 0)
        
        # Only try text decoding if frame is NOT encrypted OR was successfully decrypted
        if not is_encrypted or frame.get('decrypted'):
            try:
                printable_count = sum(1 for b in data if 32 <= b <= 126 or b in (10, 13, 9))
                # Increased threshold to 0.85 and require minimum length to avoid garbage
                if len(data) >= 5 and (printable_count / len(data)) >= 0.85:
                    text = data.decode('latin-1', errors='replace')
                    text = ''.join(c if (32 <= ord(c) <= 126 or c in '\n\r\t') else '' for c in text)
                    text = text.strip()
                    # More strict: require letters and minimum length
                    if len(text) >= 3 and any(c.isalpha() for c in text):
                        return f"[TXT] {text}"
            except:
                pass
        
        return None

    def _row_matches_filter(self, row: int, filter_type: str | None = None, decrypted_only: bool | None = None) -> bool:
        """Return True if frames_table row should be visible under current filters."""
        if filter_type is None:
            filter_type = self.type_filter.currentText().lower()
        else:
            filter_type = filter_type.lower()
        if decrypted_only is None:
            decrypted_only = self.decrypted_only_cb.isChecked()

        type_item = self.frames_table.item(row, 2)
        if not type_item:
            return False

        type_text = type_item.text().lower()
        visible = True

        if decrypted_only:
            status_item = self.frames_table.item(row, 6)
            data_item = self.frames_table.item(row, 7)
            message_item = self.frames_table.item(row, 4)
            encrypted_item = self.frames_table.item(row, 5)

            is_clear_frame = encrypted_item and encrypted_item.text().strip().lower() in ["no", "clear"]
            is_successfully_decrypted = (
                status_item
                and ("✅" in status_item.text() or "decrypted" in status_item.text().lower())
                and "encrypted" not in status_item.text().lower()
            )

            has_readable_text = False
            text_candidates = []
            if message_item and message_item.text().strip():
                text_candidates.append(message_item.text())
            if data_item and data_item.text().strip():
                text_candidates.append(data_item.text())
            for candidate in text_candidates:
                if "[BIN-ENC]" in candidate or "SDS (Binary/Encrypted)" in candidate:
                    continue
                if any(marker in candidate for marker in ["[TXT]", "[SDS-1]", "[SDS-GSM]", "[GSM7]", "📝", "💬", "🧩"]):
                    clean_text = candidate
                    for marker in ["[TXT]", "[SDS-1]", "[SDS-GSM]", "[GSM7]", "📝", "💬", "🧩", "\""]:
                        clean_text = clean_text.replace(marker, "")
                    clean_text = clean_text.strip()
                    if len(clean_text) >= 3 and any(c.isalpha() for c in clean_text):
                        hex_chars = sum(1 for c in clean_text if c in "0123456789abcdefABCDEF ")
                        if hex_chars / len(clean_text) < 0.8:
                            has_readable_text = True
                            break

            visible = bool(is_clear_frame or is_successfully_decrypted or has_readable_text)
            if visible and encrypted_item and "yes" in encrypted_item.text().lower():
                visible = bool(is_successfully_decrypted or has_readable_text)

        if not visible:
            return False

        if filter_type.startswith("all"):
            return True

        data_item = self.frames_table.item(row, 7)
        data_text = data_item.text().lower() if data_item else ""
        desc_item = self.frames_table.item(row, 3)
        desc_text = desc_item.text().lower() if desc_item else ""
        message_item = self.frames_table.item(row, 4)
        message_text = message_item.text().lower() if message_item else ""

        if "traffic" in filter_type:
            # Traffic channel / voice; type_name is often MAC-FRAG not "Traffic"
            return (
                "traffic" in type_text
                or "voice" in data_text
                or "tch" in type_text
                or "mac-frag" in type_text
            )
        if "control" in filter_type:
            return any(
                key in type_text
                for key in ("control", "resource", "u-signal", "mac-end", "mac-data", "suppl")
            )
        if "broadcast" in filter_type:
            return "broadcast" in type_text
        if filter_type.strip() == "mac" or filter_type.startswith("mac"):
            return "mac" in type_text
        if "sds" in filter_type:
            return (
                "sds" in type_text
                or "data" in type_text
                or "suppl" in type_text
                or "sns" in type_text
                or "sds" in desc_text
                or "text" in desc_text
                or "sds" in message_text
                or "text" in message_text
                or "[bin" in message_text
            )
        if "audio" in filter_type:
            return "voice" in data_text or "audio" in data_text

        return True

    def apply_filter(self, text):
        """Apply type filter to all rows (existing and newly inserted)."""
        filter_type = text.lower() if text else self.type_filter.currentText().lower()
        decrypted_only = self.decrypted_only_cb.isChecked()
        for row in range(self.frames_table.rowCount()):
            visible = self._row_matches_filter(row, filter_type, decrypted_only)
            self.frames_table.setRowHidden(row, not visible)

    def apply_calls_filter(self):
        """Apply filters to calls table."""
        group_filter = self.calls_group_filter.currentText().lower()
        if group_filter == "all": group_filter = ""
        
        client_filter = self.calls_client_filter.currentText().lower()
        if client_filter == "all": client_filter = ""
        
        for row in range(self.calls_table.rowCount()):
            visible = True
            
            # Check Group (To column often contains TG)
            if group_filter:
                to_item = self.calls_table.item(row, 8)
                if not to_item or group_filter not in to_item.text().lower():
                    visible = False
            
            # Check Client (From or To)
            if visible and client_filter:
                from_item = self.calls_table.item(row, 7)
                to_item = self.calls_table.item(row, 8)
                
                from_match = from_item and client_filter in from_item.text().lower()
                to_match = to_item and client_filter in to_item.text().lower()
                
                if not (from_match or to_match):
                    visible = False
            
            self.calls_table.setRowHidden(row, not visible)

    def apply_users_filter(self):
        """Apply filters to users table."""
        group_filter = self.users_group_filter.currentText().lower()
        if group_filter == "all": group_filter = ""
        
        for row in range(self.users_table.rowCount()):
            visible = True
            
            if group_filter:
                gssi_item = self.users_table.item(row, 2)
                if not gssi_item or group_filter not in gssi_item.text().lower():
                    visible = False
            
            self.users_table.setRowHidden(row, not visible)

    def update_filter_dropdowns(self, new_call_group=None, new_call_client=None, new_user_group=None):
        """Update filter dropdowns with new values."""
        if new_call_group and new_call_group not in self.known_call_groups:
            self.known_call_groups.add(new_call_group)
            self.calls_group_filter.addItem(new_call_group)
            
        if new_call_client and new_call_client not in self.known_call_clients:
            self.known_call_clients.add(new_call_client)
            self.calls_client_filter.addItem(new_call_client)
            
        if new_user_group and new_user_group not in self.known_user_groups:
            self.known_user_groups.add(new_user_group)
            self.users_group_filter.addItem(new_user_group)

    def update_tables(self, frame):
        """Update Calls, Groups, and Users tables."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        # Extract metadata
        meta = frame.get('call_metadata', {})
        if not meta and 'additional_info' in frame:
            # Map additional_info to meta structure
            info = frame['additional_info']
            if 'talkgroup' in info: meta['talkgroup_id'] = info['talkgroup']
            if 'source_ssi' in info: meta['source_ssi'] = info['source_ssi']
            if 'dest_ssi' in info: meta['dest_ssi'] = info['dest_ssi']
            if 'control' in info: meta['call_type'] = info['control']
            if 'mcc' in info: meta['mcc'] = info['mcc']
            if 'mnc' in info: meta['mnc'] = info['mnc']
        
        # Helper to format None as empty string
        def fmt(val):
            return str(val) if val is not None else ""

        # Update Calls Table
        # Columns: Time, MCarrier, Carrier, Slot, CallID, Pri, Type, From, To, Mode
        if meta.get('call_type') or meta.get('source_ssi') or meta.get('dest_ssi') or meta.get('talkgroup_id'):
            row = self.calls_table.rowCount()
            self.calls_table.insertRow(row)
            self.calls_table.setItem(row, 0, QTableWidgetItem(timestamp))
            
            # MCarrier (Main Carrier) - Use tuned frequency if not specified
            mcarrier = fmt(frame.get('frequency'))
            if not mcarrier and hasattr(self, 'freq_input'):
                mcarrier = self.freq_input.text()
            self.calls_table.setItem(row, 1, QTableWidgetItem(mcarrier)) # MCarrier
            
            self.calls_table.setItem(row, 2, QTableWidgetItem(fmt(meta.get('channel')))) # Carrier
            self.calls_table.setItem(row, 3, QTableWidgetItem(fmt(frame.get('timeslot')))) # Slot
            self.calls_table.setItem(row, 4, QTableWidgetItem(fmt(meta.get('call_identifier')))) # CallID
            self.calls_table.setItem(row, 5, QTableWidgetItem(fmt(meta.get('priority')))) # Pri
            self.calls_table.setItem(row, 6, QTableWidgetItem(fmt(meta.get('call_type')))) # Type
            self.calls_table.setItem(row, 7, QTableWidgetItem(fmt(meta.get('source_ssi')))) # From
            
            # To field: Dest SSI or Talkgroup
            to_val = fmt(meta.get('dest_ssi'))
            if not to_val and meta.get('talkgroup_id'):
                to_val = f"TG:{meta['talkgroup_id']}"
            self.calls_table.setItem(row, 8, QTableWidgetItem(to_val)) # To
            
            mode = "Encrypted" if frame.get('encrypted') else "Clear"
            if frame.get('has_voice'): mode += " (Voice)"
            if frame.get('decrypted'): mode += " [DEC]"
            self.calls_table.setItem(row, 9, QTableWidgetItem(mode)) # Mode
            
            if self.autoscroll_cb.isChecked():
                self.calls_table.scrollToBottom()

        # Update Groups Table (if GSSI present)
        # Columns: GSSI, LO, REC, MCC, MNC, Priority, Name
        if meta.get('talkgroup_id'):
            gssi = str(meta['talkgroup_id'])
            # Check if already exists
            found = False
            for r in range(self.groups_table.rowCount()):
                item = self.groups_table.item(r, 0)
                if item and item.text() == gssi:
                    found = True
                    # Update Last Seen (LO)
                    self.groups_table.setItem(r, 1, QTableWidgetItem(timestamp))
                    # Update REC status
                    rec_status = "🔴" if self.recording_active else ""
                    self.groups_table.setItem(r, 2, QTableWidgetItem(rec_status))
                    
                    # Update other fields if they were empty
                    if not self.groups_table.item(r, 3).text():
                        self.groups_table.setItem(r, 3, QTableWidgetItem(fmt(meta.get('mcc'))))
                    if not self.groups_table.item(r, 4).text():
                        self.groups_table.setItem(r, 4, QTableWidgetItem(fmt(meta.get('mnc'))))
                    
                    # Update Name/Country column
                    mcc = meta.get('mcc')
                    mnc = meta.get('mnc')
                    if mcc:
                        country_str = get_location_info(str(mcc), str(mnc) if mnc else None)
                        name_str = f"👥 Group {gssi} ({country_str})"
                        self.groups_table.setItem(r, 6, QTableWidgetItem(name_str))
                    break
            
            if not found:
                row = self.groups_table.rowCount()
                self.groups_table.insertRow(row)
                self.groups_table.setItem(row, 0, QTableWidgetItem(gssi))
                self.groups_table.setItem(row, 1, QTableWidgetItem(timestamp)) # Last Seen
                
                rec_status = "🔴" if self.recording_active else ""
                self.groups_table.setItem(row, 2, QTableWidgetItem(rec_status)) # REC
                
                self.groups_table.setItem(row, 3, QTableWidgetItem(fmt(meta.get('mcc')))) # MCC
                self.groups_table.setItem(row, 4, QTableWidgetItem(fmt(meta.get('mnc')))) # MNC
                self.groups_table.setItem(row, 5, QTableWidgetItem(fmt(meta.get('priority')))) # Priority
                
                # Name/Country column (combined)
                mcc = meta.get('mcc')
                mnc = meta.get('mnc')
                if mcc:
                    country_str = get_location_info(str(mcc), str(mnc) if mnc else None)
                    name_str = f"👥 Group {gssi} ({country_str})"
                else:
                    name_str = f"👥 Group {gssi}"
                self.groups_table.setItem(row, 6, QTableWidgetItem(name_str)) # Name/Country

        # Update Users Table (if ISSI present)
        # Columns: ISSI, LO, GSSI, MCC, MNC, Name, Location
        if meta.get('source_ssi'):
            issi = str(meta['source_ssi'])
            # Check if already exists
            found = False
            for r in range(self.users_table.rowCount()):
                item = self.users_table.item(r, 0)
                if item and item.text() == issi:
                    found = True
                    # Update Last Seen
                    self.users_table.setItem(r, 1, QTableWidgetItem(timestamp))
                    # Update GSSI if present
                    if meta.get('talkgroup_id'):
                        self.users_table.setItem(r, 2, QTableWidgetItem(str(meta['talkgroup_id'])))
                    
                    # Update MCC/MNC if present
                    if meta.get('mcc'): self.users_table.setItem(r, 3, QTableWidgetItem(str(meta['mcc'])))
                    if meta.get('mnc'): self.users_table.setItem(r, 4, QTableWidgetItem(str(meta['mnc'])))
                    
                    # Update Location/Country column (combined)
                    loc_str = ""
                    
                    # Try to extract GPS coordinates
                    gps_data = LocationParser.extract_location_from_frame(frame)
                    if gps_data:
                        loc_str = f"📍 {gps_data['formatted']}"
                    else:
                        # No GPS, show country from MCC/MNC
                        mcc = meta.get('mcc')
                        mnc = meta.get('mnc')
                        if mcc:
                            loc_str = get_location_info(str(mcc), str(mnc) if mnc else None)
                    
                    if loc_str:
                        self.users_table.setItem(r, 6, QTableWidgetItem(loc_str))
                    break
            
            if not found:
                row = self.users_table.rowCount()
                self.users_table.insertRow(row)
                self.users_table.setItem(row, 0, QTableWidgetItem(issi))
                self.users_table.setItem(row, 1, QTableWidgetItem(timestamp)) # Last Seen
                self.users_table.setItem(row, 2, QTableWidgetItem(fmt(meta.get('talkgroup_id')))) # GSSI
                self.users_table.setItem(row, 3, QTableWidgetItem(fmt(meta.get('mcc')))) # MCC
                self.users_table.setItem(row, 4, QTableWidgetItem(fmt(meta.get('mnc')))) # MNC
                self.users_table.setItem(row, 5, QTableWidgetItem(f"👤 User {issi}")) # Name with icon
                
                # Location/Country column (combined)
                loc_str = ""
                
                # Try to extract GPS coordinates
                gps_data = LocationParser.extract_location_from_frame(frame)
                if gps_data:
                    loc_str = f"📍 {gps_data['formatted']}"
                else:
                    # No GPS, show country from MCC/MNC
                    mcc = meta.get('mcc')
                    mnc = meta.get('mnc')
                    if mcc:
                        loc_str = get_location_info(str(mcc), str(mnc) if mnc else None)
                
                self.users_table.setItem(row, 6, QTableWidgetItem(loc_str)) # Location/Country

        # Update filters
        call_group = None
        if meta.get('dest_ssi'):
            call_group = str(meta['dest_ssi'])
        elif meta.get('talkgroup_id'):
            call_group = f"TG:{meta['talkgroup_id']}"
            
        call_client = str(meta['source_ssi']) if meta.get('source_ssi') else None
        user_group = str(meta['talkgroup_id']) if meta.get('talkgroup_id') else None
        
        self.update_filter_dropdowns(call_group, call_client, user_group)

    @pyqtSlot(dict)
    def on_frame(self, frame):
        """Handle decoded frame."""
        self.frame_count += 1

        # Per-frame JSONL only with --debug (default off — huge I/O otherwise).
        if _DEBUG_FRAME_LOG:
            try:
                import json

                frames_logger.info(
                    json.dumps(
                        {
                            "event": "frame_decoded",
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "frame_number": frame.get("number"),
                            "type": frame.get("type"),
                            "type_name": frame.get("type_name"),
                            "encrypted": bool(frame.get("encrypted")),
                            "encryption_algorithm": frame.get("encryption_algorithm"),
                            "decrypted": bool(frame.get("decrypted")),
                            "decryption_attempted": bool(frame.get("decryption_attempted")),
                            "keys_tried": frame.get("keys_tried"),
                            "best_score": frame.get("best_score"),
                            "best_key": frame.get("best_key"),
                            "key_used": frame.get("key_used"),
                            "decrypt_confidence": frame.get("decrypt_confidence"),
                            "decryption_error": frame.get("decryption_error"),
                            "additional_info": frame.get("additional_info", {}),
                            "sds_message": frame.get("sds_message"),
                            "decoded_text": frame.get("decoded_text"),
                            "has_voice": bool(frame.get("has_voice")),
                        },
                        ensure_ascii=False,
                    )
                )
            except Exception:
                pass
        
        # VALIDATE FRAME: Check if this is real TETRA
        is_valid, confidence, issues = self.signal_validator.validate_frame(frame)
        
        if not is_valid:
            # Log invalid frames but don't display them / don't pollute Groups
            logger.debug(f"Invalid TETRA frame: confidence={confidence:.1%}, issues={issues}")
            return  # Skip this frame
        
        if confidence < 0.7:
            # Low confidence frame - log warning
            logger.warning(f"Low confidence TETRA frame: {confidence:.1%}, issues={issues}")
        
        # Update tables regardless of auto-decrypt setting.
        self.update_tables(frame)
        
        # Try SDS reassembly first
        reassembled_sds = self.reassemble_sds_message(frame)
        if reassembled_sds:
            frame['sds_message'] = reassembled_sds
            frame['is_reassembled'] = True
        
        # Check if this is test data FIRST - don't count test frames for TETRA detection
        is_test = frame.get('is_test_data', False)
        
        # Track TETRA signal detection (only for real frames, not test data)
        # Count ALL decoded frames as potential TETRA frames (if decoder found them, they're likely TETRA)
        if not is_test:
            import time
            # Count all non-test frames as potential TETRA frames
            # If the decoder found and decoded a frame, it's likely a valid TETRA frame
            self.tetra_frame_count += 1
            
            # Check for sync position (indicates sync pattern was found)
            if frame.get('position') is not None:
                self.tetra_sync_count += 1
            
            # Check for valid CRC (prefer real lower-MAC crc_ok)
            if frame.get('crc_ok') is True:
                self.tetra_valid_frames += 1
            elif frame.get('burst_crc') is True:
                self.tetra_valid_frames += 1
            elif frame.get('decrypted') or frame.get('bypass_clear'):
                self.tetra_valid_frames += 1

            # Cell lock from SYNC SB1 / SYSINFO
            info = frame.get("additional_info") or {}
            if frame.get("mcc") is not None:
                self.cell_mcc = frame.get("mcc")
            elif info.get("mcc") is not None:
                self.cell_mcc = info.get("mcc")
            if frame.get("mnc") is not None:
                self.cell_mnc = frame.get("mnc")
            elif info.get("mnc") is not None:
                self.cell_mnc = info.get("mnc")
            if frame.get("colour_code") is not None:
                self.cell_colour = frame.get("colour_code")
            elif info.get("colour_code") is not None:
                self.cell_colour = info.get("colour_code")
            if frame.get("location_area") is not None:
                self.cell_la = frame.get("location_area")
            elif info.get("location_area") is not None:
                self.cell_la = info.get("location_area")
            
            # Track when first frame was detected
            if self.tetra_frame_count == 1:
                self.first_frame_time = time.time()
            
            # Update TETRA status with debouncing to prevent rapid changes
            # Wait at least min_detection_time before showing "TETRA Signal Detected"
            current_time = time.time()
            time_since_first = current_time - (self.first_frame_time or current_time)
            
            # Update status more frequently when frames are being decoded
            # Update if enough time has passed since first frame AND enough time since last update
            # Also update more frequently when we have many frames (likely valid signal)
            should_update = (
                time_since_first >= self.min_detection_time and  # Wait for minimum detection time
                (current_time - self.last_tetra_update > 2.0 or  # 2 seconds since last update (faster)
                 self.tetra_frame_count % 5 == 0 or  # Or every 5 frames (more frequent)
                 (self.tetra_frame_count >= 10 and current_time - self.last_tetra_update > 1.0))  # Or every 1s when many frames
            )
            
            if should_update:
                self.update_tetra_status()
                self.last_tetra_update = current_time
        
        if frame.get('decrypted'):
            self.decrypted_count += 1
        
        type_name = frame.get('type_name', "Unknown")
        # Cap table size — unbounded growth under CRC-fail floods can OOM/abort
        max_rows = 2000
        while self.frames_table.rowCount() >= max_rows:
            self.frames_table.removeRow(0)
        row = self.frames_table.rowCount()
        self.frames_table.insertRow(row)
        
        # Determine row color based on frame type - Enhanced colors
        row_bg = None
        text_color = None
        
        if "MAC-RESOURCE" in type_name:
            row_bg = QColor(30, 50, 80)  # Bright Blue
            text_color = QColor(200, 220, 255)  # Light blue text
        elif "MAC-BROADCAST" in type_name:
            row_bg = QColor(80, 70, 30)  # Bright Yellow/Orange
            text_color = QColor(255, 240, 200)  # Light yellow text
        elif "MAC-FRAG" in type_name:
            row_bg = QColor(30, 80, 30)  # Bright Green
            text_color = QColor(200, 255, 200)  # Light green text
        elif "MAC-SUPPL" in type_name:
            row_bg = QColor(80, 30, 80)  # Bright Purple
            text_color = QColor(255, 200, 255)  # Light purple text
        elif "MAC-U-SIGNAL" in type_name:
            row_bg = QColor(100, 30, 30)  # Bright Red
            text_color = QColor(255, 200, 200)  # Light red text
        elif "MAC-DATA" in type_name:
            row_bg = QColor(30, 80, 100)  # Bright Cyan
            text_color = QColor(200, 255, 255)  # Light cyan text
        elif frame.get('has_voice'):
            row_bg = QColor(0, 120, 0)  # Bright Green for voice
            text_color = QColor(200, 255, 200)  # Light green text
        elif 'sds_message' in frame or 'decoded_text' in frame:
            row_bg = QColor(0, 100, 120)  # Bright Cyan for SDS
            text_color = QColor(200, 255, 255)  # Light cyan text
            
        def create_item(text, color=None):
            item = QTableWidgetItem(str(text))
            if is_test:
                item.setForeground(QColor(128, 128, 128))
            elif color:
                item.setForeground(color)
            elif text_color:
                item.setForeground(text_color)
            
            if row_bg:
                item.setBackground(row_bg)
            return item

        time_str = datetime.now().strftime("%H:%M:%S")
        self.frames_table.setItem(row, 0, create_item(time_str))
        
        self.frames_table.setItem(row, 1, create_item(frame['number']))
        
        self.frames_table.setItem(row, 2, create_item(type_name))
        
        # Build description with all available metadata
        desc = frame.get('additional_info', {}).get('description', '')
        
        # Check call_metadata first (from real decoder)
        if 'call_metadata' in frame:
            meta = frame['call_metadata']
            if meta.get('talkgroup_id'):
                desc += f" | TG:{meta['talkgroup_id']}"
            if meta.get('source_ssi'):
                desc += f" | Src:{meta['source_ssi']}"
            if meta.get('dest_ssi'):
                desc += f" | Dst:{meta['dest_ssi']}"
            if meta.get('call_type'):
                desc += f" | {meta['call_type']}"
        
        # Fallback to additional_info (from synthetic or other parts)
        elif 'additional_info' in frame:
            info = frame['additional_info']
            if 'talkgroup' in info:
                desc += f" | TG:{info['talkgroup']}"
            if 'source_ssi' in info:
                desc += f" | Src:{info['source_ssi']}"
            if 'dest_ssi' in info:
                desc += f" | Dst:{info['dest_ssi']}"
            if 'channel' in info:
                desc += f" | Ch:{info['channel']}"
            if 'control' in info:
                desc += f" | {info['control']}"
        
        message_text = ""
        # Check for readable message text
        text_to_check = frame.get('sds_message') or frame.get('decoded_text') or ''
        if text_to_check:
            if _is_readable_text(text_to_check):
                # Clean and show readable text
                clean = text_to_check
                for prefix in ['[GSM7]', '[TXT]', '[SDS]', '[SDS-1]', '[SDS-GSM]', '💬', '🧩']:
                    clean = clean.replace(prefix, '')
                clean = clean.strip().strip('"')
                prefix_icon = "🧩 " if frame.get('is_reassembled') else "💬 "
                message_text = f"{prefix_icon}\"{clean.strip()[:80]}\""
            else:
                # Check if it's location/metadata
                location = _format_location_data(frame)
                if location:
                    message_text = location
                else:
                    metadata = _format_binary_metadata(frame)
                    if metadata:
                        message_text = metadata
                    else:
                        # Don't show garbled text - just show "Decrypted" if decrypted
                        if frame.get('decrypted'):
                            message_text = "✅ Decrypted"
                        else:
                            message_text = "🔒 (Encrypted)"
        
        # Mark test data
        if is_test:
            desc = "[TEST] " + desc
        
        self.frames_table.setItem(row, 3, create_item(desc))
        self.frames_table.setItem(row, 4, create_item(message_text))
        
        enc_text = "Yes" if frame.get('encrypted') else "No"
        if frame.get("encryption_suspected") and not frame.get("encrypted"):
            enc_text = "Suspected"

        if frame.get('encrypted') or frame.get("encryption_suspected"):
            alg = frame.get('encryption_algorithm') or 'Unknown'
            enc_text += f" ({alg})"
        
        if frame.get('encrypted') or frame.get("encryption_suspected"):
            enc_color = QColor(255, 100, 100)
        else:
            enc_color = QColor(100, 255, 100)
        self.frames_table.setItem(row, 5, create_item(enc_text, enc_color))
        
        status_text = ""
        status_color = None
        if frame.get('decrypted'):
            confidence = frame.get('decrypt_confidence', 0)
            key_used = frame.get('key_used', 'unknown')
            status_text = f"✅ Decrypted ({confidence}) | {key_used}"
            status_color = QColor(0, 255, 255)
        elif frame.get("bypass_clear"):
            status_text = "🟢 Clear (Bypass)"
            status_color = QColor(100, 255, 100)
        elif frame.get("decryption_attempted"):
            keys_tried = frame.get("keys_tried", 0)
            best_score = frame.get("best_score", 0)
            best_key = frame.get("best_key") or "n/a"
            status_text = f"🔑 Bruteforce tried {keys_tried} | best {best_score} | {best_key}"
            status_color = QColor(200, 200, 120)
        elif frame.get('encrypted'):
            alg = frame.get('encryption_algorithm') or 'Unknown'
            if not self.auto_decrypt_cb.isChecked():
                status_text = f"🔒 Encrypted ({alg}) | Auto-Decrypt off"
            else:
                status_text = f"🔒 Encrypted ({alg})"
            status_color = QColor(255, 165, 0)
        else:
            status_text = "Clear"
            
        self.frames_table.setItem(row, 6, create_item(status_text, status_color))
        
        # Data Column - Prioritize decoded text but filter garbled
        data_str = ""
        if frame.get('has_voice'):
             data_str = "🔊 Voice Audio (Decoded)"
        else:
            # Check for location data first
            location_str = _format_location_data(frame)
            if location_str:
                data_str = location_str
            # Check for readable text
            elif 'decoded_text' in frame and frame['decoded_text']:
                text = frame['decoded_text']
                if _is_readable_text(text):
                    # Clean text - remove prefix
                    clean = text
                    for prefix in ['[GSM7]', '[TXT]', '[SDS]', '[SDS-1]', '[SDS-GSM]', '💬', '🧩']:
                        clean = clean.replace(prefix, '')
                    clean = clean.strip().strip('"')
                    data_str = f"💬 {clean.strip()[:80]}"
                else:
                    # Check if it's binary/metadata
                    metadata_str = _format_binary_metadata(frame)
                    if metadata_str:
                        data_str = metadata_str
                    else:
                        # Garbled text - just say decrypted if it was
                        if frame.get('decrypted'):
                            data_str = "✅ Decrypted (garbled)"
                        else:
                            data_str = "🔒 (Encrypted)"
            elif 'sds_message' in frame and frame['sds_message']:
                text = frame['sds_message']
                if _is_readable_text(text):
                    clean = text
                    for prefix in ['[GSM7]', '[TXT]', '[SDS]', '[SDS-1]', '[SDS-GSM]', '💬', '🧩']:
                        clean = clean.replace(prefix, '')
                    clean = clean.strip().strip('"')
                    data_str = f"💬 {clean.strip()[:80]}"
                else:
                    metadata_str = _format_binary_metadata(frame)
                    if metadata_str:
                        data_str = metadata_str
                    else:
                        if frame.get('decrypted'):
                            data_str = "✅ Decrypted (garbled)"
                        else:
                            data_str = "🔒 (Encrypted)"
            elif 'decrypted_bytes' in frame:
                # Try to parse decrypted bytes as text if printable
                try:
                    data_bytes = bytes.fromhex(frame['decrypted_bytes'])
                    if len(data_bytes) > 0:
                        # Try to decode as text
                        text = data_bytes.decode('latin-1', errors='replace')
                        # Check if text is actually readable
                        if _is_readable_text(text):
                            clean = text.strip()
                            data_str = f"💬 {clean[:80]}"
                        else:
                            # Not readable - show as decrypted but garbled
                            if frame.get('decrypted'):
                                data_str = "✅ Decrypted (garbled)"
                            else:
                                data_str = "🔒 (Binary data)"
                    else:
                        data_str = "✅ Decrypted (empty)"
                except Exception as e:
                    logger.debug(f"Error parsing decrypted bytes: {e}")
                    data_str = "✅ Decrypted (parse error)"
            elif 'mac_pdu' in frame and 'data' in frame['mac_pdu']:
                data = frame['mac_pdu']['data']
                if isinstance(data, (bytes, bytearray)):
                    # Try to decode as text
                    try:
                        text = data.decode('latin-1', errors='replace')
                        if _is_readable_text(text):
                            clean = text.strip()
                            data_str = f"💬 {clean[:80]}"
                        else:
                            # Binary or garbled data
                            if frame.get('decrypted'):
                                data_str = "✅ Decrypted (garbled)"
                            else:
                                data_str = "🔒 (Binary data)"
                    except:
                        data_str = "🔒 (Binary data)"
                else:
                    # Non-bytes data - check if readable
                    text = str(data)
                    if _is_readable_text(text):
                        data_str = f"💬 {text[:80]}"
                    else:
                        data_str = "🔒 (Binary data)"
            elif 'bits' in frame:
                # Raw bits - try to decode but filter garbled
                try:
                    bits = frame['bits']
                    if hasattr(bits, 'tobytes'):
                        data_bytes = bits.tobytes()
                        if len(data_bytes) > 0:
                            text = data_bytes.decode('latin-1', errors='replace')
                            if _is_readable_text(text):
                                clean = text.strip()
                                data_str = f"💬 {clean[:80]}"
                            else:
                                # Binary data - don't show hex
                                data_str = "🔒 (Binary data)"
                        else:
                            data_str = "(empty)"
                    else:
                        data_str = "🔒 (Binary data)"
                except:
                    data_str = "🔒 (Binary data)"
            else:
                data_str = ""
                  
        self.frames_table.setItem(row, 7, create_item(data_str))
        
        # Country column (new) - extract from MCC/MNC
        country_str = ""
        if 'call_metadata' in frame:
            meta = frame['call_metadata']
            mcc = meta.get('mcc')
            mnc = meta.get('mnc')
            if mcc:
                country_str = get_location_info(str(mcc), str(mnc) if mnc else None)
        elif 'additional_info' in frame:
            info = frame['additional_info']
            mcc = info.get('mcc')
            mnc = info.get('mnc')
            if mcc:
                country_str = get_location_info(str(mcc), str(mnc) if mnc else None)
        
        self.frames_table.setItem(row, 8, create_item(country_str))

        # Apply current type / decrypted filters to this new row (same rules as existing ones)
        try:
            visible = self._row_matches_filter(row)
            self.frames_table.setRowHidden(row, not visible)
        except Exception:
            pass

        # Persist full frames table row to JSONL only in --debug mode.
        if _DEBUG_FRAME_LOG:
            try:
                import json

                frames_logger.info(
                    json.dumps(
                        {
                            "time": time_str,
                            "frame_number": frame.get("number"),
                            "type": type_name,
                            "description": desc,
                            "message": message_text,
                            "encrypted": bool(frame.get("encrypted")),
                            "encryption_algorithm": frame.get("encryption_algorithm"),
                            "decrypted": bool(frame.get("decrypted")),
                            "status": status_text,
                            "data": data_str,
                            "keys_tried": frame.get("keys_tried"),
                            "best_score": frame.get("best_score"),
                            "best_key": frame.get("best_key"),
                            "key_used": frame.get("key_used"),
                            "decrypt_confidence": frame.get("decrypt_confidence"),
                            "sds_message": frame.get("sds_message"),
                            "decoded_text": frame.get("decoded_text"),
                        },
                        ensure_ascii=False,
                    )
                )
            except Exception:
                pass
        
        # Auto-scroll
        if self.autoscroll_cb.isChecked():
            self.frames_table.scrollToBottom()

    
    @pyqtSlot(str)
    def on_error(self, msg):
        """Handle error."""
        self.log(f"ERROR: {msg}", "red")
    
    @pyqtSlot(str)
    def on_status(self, msg):
        """Handle status."""
        self.log(msg)
        self.statusBar().showMessage(msg)
    
    @pyqtSlot(np.ndarray, np.ndarray)
    def on_spectrum(self, freqs, powers):
        """Handle spectrum update."""
        self.waterfall.update_spectrum(freqs, powers)
        if self.auto_spectrum_enabled:
            self._apply_auto_spectrum(freqs, powers)
        # Do NOT re-read freq_input here: mid-edit / post-Tune text can move the
        # listening bars off-screen or clear them. Bars follow the last committed
        # Tune/Start (and AFC below) only.
        try:
            # Prefer committed listening frequency; fall back to input if unset
            if getattr(self.waterfall, "tuned_freq", None) is not None:
                freq_mhz = float(self.waterfall.tuned_freq)
            else:
                freq_mhz = float(self.freq_input.text())
                self.waterfall.set_tuned_frequency(freq_mhz)

            # Auto-follow frequency logic (AFC)
            if hasattr(self, 'follow_freq_cb') and self.follow_freq_cb.isChecked():
                # Spectrum freqs are in Hz; compare in Hz.
                freq_hz = freq_mhz * 1e6
                window_width_hz = 10_000.0  # ±10 kHz
                mask = np.abs(freqs - freq_hz) < window_width_hz

                if np.any(mask):
                    window_indices = np.where(mask)[0]
                    peak_idx = window_indices[int(np.argmax(powers[window_indices]))]
                    peak_freq_hz = float(freqs[peak_idx])
                    peak_power = float(powers[peak_idx])
                    offset_hz = peak_freq_hz - freq_hz

                    if peak_power > -60 and abs(offset_hz) > 50.0:
                        new_freq_mhz = freq_mhz + (offset_hz * 0.1) / 1e6
                        self.freq_input.setText(f"{new_freq_mhz:.6f}")
                        self.waterfall.set_tuned_frequency(new_freq_mhz)
                        if self.capture_thread:
                            self.capture_thread.set_frequency(int(new_freq_mhz * 1e6))
        except Exception:
            pass
    
    def update_displays(self):
        """Update displays."""
        self.decrypt_label.setText(f"🔒 {self.decrypted_count}/{self.frame_count}")
        
        # Update recording status
        if self.recording_active and self.recording_start_time:
            duration = datetime.now() - self.recording_start_time
            seconds = int(duration.total_seconds())
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            secs = seconds % 60
            
            size_str = "0 B"
            if hasattr(self, 'current_wav_file') and os.path.exists(self.current_wav_file):
                size_bytes = os.path.getsize(self.current_wav_file)
                if size_bytes > 1024*1024:
                    size_str = f"{size_bytes/(1024*1024):.1f} MB"
                else:
                    size_str = f"{size_bytes/1024:.1f} KB"
            
            self.recording_status_label.setText(f"🔴 LIVE | {hours:02}:{minutes:02}:{secs:02} | {size_str}")
            self.recording_status_label.setStyleSheet("font-weight: bold; padding: 5px; color: #ff0000; background-color: #220000;")
        else:
            self.recording_status_label.setText("⚫ Not Recording")
            self.recording_status_label.setStyleSheet("font-weight: bold; padding: 5px; color: #888888;")
        
        # Update recording status
        if self.recording_active and self.recording_start_time:
            duration = datetime.now() - self.recording_start_time
            seconds = int(duration.total_seconds())
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            secs = seconds % 60
            
            size_str = "0 B"
            if hasattr(self, 'current_wav_file') and os.path.exists(self.current_wav_file):
                size_bytes = os.path.getsize(self.current_wav_file)
                if size_bytes > 1024*1024:
                    size_str = f"{size_bytes/(1024*1024):.1f} MB"
                else:
                    size_str = f"{size_bytes/1024:.1f} KB"
            
            self.recording_status_label.setText(f"🔴 LIVE | {hours:02}:{minutes:02}:{secs:02} | {size_str}")
            self.recording_status_label.setStyleSheet("font-weight: bold; padding: 5px; color: #ff0000; background-color: #220000;")
        else:
            self.recording_status_label.setText("⚫ Not Recording")
            self.recording_status_label.setStyleSheet("font-weight: bold; padding: 5px; color: #888888;")
    
    def update_tetra_status(self):
        """Update TETRA signal detection status indicator."""
        import time
        
        # Only show TETRA detected if we have both signal and validated frames
        # This prevents showing TETRA detected when signal is lost
        if not self.signal_present or self.tetra_frame_count == 0:
            self.tetra_status_label.setText("⚫ No TETRA Signal")
            self.tetra_status_label.setStyleSheet(
                "font-weight: bold; padding: 5px; color: #888888;"
            )
            self.first_frame_time = None  # Reset when no frames
            return
        
        # Check if minimum detection time has passed
        current_time = time.time()
        time_since_first = current_time - (self.first_frame_time or current_time)
        
        # Don't show "TETRA Signal Detected" until minimum time has passed
        if time_since_first < self.min_detection_time:
            # Show "decoding" status during the wait period
            self.tetra_status_label.setText(f"🟡 Signal Detected (Analyzing... {int(self.min_detection_time - time_since_first)}s)")
            self.tetra_status_label.setStyleSheet(
                "font-weight: bold; padding: 5px; color: #ffaa00; background-color: #221100;"
            )
            return
        
        # Calculate detection metrics
        sync_rate = self.tetra_sync_count / max(self.tetra_frame_count, 1)
        crc_rate = self.tetra_valid_frames / max(self.tetra_frame_count, 1)
        
        # More lenient detection: if frames are being decoded successfully, they're likely TETRA
        # Decrypted frames or frames with type information are strong indicators
        # Require at least 3 frames, and if we have validated frames (sync/CRC/decrypted), use that
        # Otherwise, if we have multiple decoded frames, count them as TETRA
        has_validated_frames = (sync_rate > 0.1 or crc_rate > 0.1)
        
        is_tetra_detected = (
            self.tetra_frame_count >= 3 and  # At least 3 frames
            (has_validated_frames or self.tetra_frame_count >= 5)  # Either validated or 5+ frames
        )
        
        # High confidence: multiple frames with good validation or many decoded frames
        high_confidence = (
            self.tetra_frame_count >= 5 and
            (sync_rate > 0.2 or crc_rate > 0.3 or self.tetra_frame_count >= 10)  # Lower threshold or many frames
        )
        
        cell_bits = []
        if self.cell_mcc is not None:
            cell_bits.append(f"MCC {self.cell_mcc}")
        if self.cell_mnc is not None:
            cell_bits.append(f"MNC {self.cell_mnc}")
        if self.cell_colour is not None:
            cell_bits.append(f"CC {self.cell_colour}")
        if self.cell_la is not None:
            cell_bits.append(f"LA {self.cell_la}")
        cell_str = (" | " + " ".join(cell_bits)) if cell_bits else ""

        if high_confidence:
            self.tetra_status_label.setText(
                f"🟢 TETRA Signal Detected ({self.tetra_frame_count} frames, Sync: {sync_rate:.0%}, CRC: {crc_rate:.0%}){cell_str}"
            )
            self.tetra_status_label.setStyleSheet(
                "font-weight: bold; padding: 5px; color: #00ff00; background-color: #002200;"
            )
        elif is_tetra_detected:
            # Medium confidence: some frames decoded
            self.tetra_status_label.setText(
                f"🟡 TETRA Detected ({self.tetra_frame_count} frames, Sync: {sync_rate:.0%}, CRC: {crc_rate:.0%}){cell_str}"
            )
            self.tetra_status_label.setStyleSheet(
                "font-weight: bold; padding: 5px; color: #ffaa00; background-color: #221100;"
            )
        else:
            self.tetra_status_label.setText("⚫ No TETRA Signal")
            self.tetra_status_label.setStyleSheet(
                "font-weight: bold; padding: 5px; color: #888888;"
            )
    
    def update_stats(self):
        """Update statistics."""
        html = f"""
        <h2>📊 Statistics</h2>
        <table style="width:100%; font-family: monospace;">
        <tr><td><b>Total Frames:</b></td><td>{self.frame_count}</td></tr>
        <tr><td><b>Decrypted:</b></td><td>{self.decrypted_count}</td></tr>
        <tr><td><b>Success Rate:</b></td><td>{(self.decrypted_count/max(1,self.frame_count)*100):.1f}%</td></tr>
        <tr><td><b>TETRA Frames:</b></td><td>{self.tetra_frame_count}</td></tr>
        <tr><td><b>Valid CRC:</b></td><td>{self.tetra_valid_frames}</td></tr>
        </table>
        """
        self.stats_text.setHtml(html)
    
    def log(self, msg, color=None):
        """Log message."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {msg}"
        
        if color:
            formatted = f'<span style="color: {color};">{formatted}</span>'
        
        self.log_text.append(formatted)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        # Also write to file logs (keep UI and logs in sync).
        try:
            logger.info("%s", msg)
        except Exception:
            pass
    
    def closeEvent(self, event):
        """Handle close."""
        # Save any active recording
        if self.recording_active:
            self.save_recording()
        
        if self.capture_thread and self.capture_thread.isRunning():
            self.capture_thread.stop()
            self.capture_thread.wait()
        event.accept()


class CLITetraListener(QObject):
    """CLI Listener for TETRA events."""
    
    def __init__(self, capture_thread):
        super().__init__()
        self.capture_thread = capture_thread
        self.start_time = datetime.now()
        self.signal_active = False  # Track signal state
        self.frame_count = 0
        
        # Connect signals
        self.capture_thread.status_update.connect(self.on_status)
        self.capture_thread.error_occurred.connect(self.on_error)
        self.capture_thread.signal_detected.connect(self.on_signal)
        self.capture_thread.signal_lost.connect(self.on_signal_lost)
        self.capture_thread.frame_decoded.connect(self.on_frame)
        
    @pyqtSlot(str)
    def on_status(self, msg):
        # Remove Unicode characters for Windows compatibility
        msg = msg.replace('✓', '[OK]').replace('✗', '[X]').replace('⏺', '[REC]').replace('⏸', '[PAUSE]')
        print(f"{Fore.CYAN}[STATUS] {msg}{Style.RESET_ALL}")
        
    @pyqtSlot(str)
    def on_error(self, msg):
        print(f"{Fore.RED}[ERROR] {msg}{Style.RESET_ALL}")
        
    @pyqtSlot(float, float)
    def on_signal(self, freq, snr):
        if not self.signal_active:  # Only show when state changes
            print(f"{Fore.GREEN}[SIGNAL] TETRA Detected at {freq/1e6:.4f} MHz (SNR: {snr:.1f} dB){Style.RESET_ALL}")
            self.signal_active = True
        
    @pyqtSlot()
    def on_signal_lost(self):
        if self.signal_active:  # Only show when state changes
            print(f"{Fore.YELLOW}[SIGNAL] Signal Lost (decoded {self.frame_count} frames){Style.RESET_ALL}")
            self.signal_active = False
        
    @pyqtSlot(dict)
    def on_frame(self, frame):
        self.frame_count += 1
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        fn = frame.get('number', '?')
        ftype = frame.get('type_name', 'Unknown')
        
        # Color coding based on frame type
        color = Fore.WHITE
        if "MAC-RESOURCE" in ftype: color = Fore.BLUE
        elif "MAC-BROADCAST" in ftype: color = Fore.YELLOW
        elif "MAC-FRAG" in ftype: color = Fore.GREEN
        elif "MAC-SUPPL" in ftype: color = Fore.MAGENTA
        elif "MAC-U-SIGNAL" in ftype: color = Fore.RED
        elif "MAC-DATA" in ftype: color = Fore.CYAN
        
        # Encryption status
        enc = ""
        if frame.get('decrypted'):
            enc = f"{Fore.GREEN}[DEC]{Style.RESET_ALL}"
        elif frame.get('encrypted'):
            enc = f"{Fore.RED}[ENC]{Style.RESET_ALL}"
            
        # Content
        content = ""
        if 'sds_message' in frame:
            content = f"{Fore.CYAN}SDS: {frame['sds_message']}{Style.RESET_ALL}"
        elif 'decoded_text' in frame:
            content = f"{Fore.CYAN}TXT: {frame['decoded_text']}{Style.RESET_ALL}"
        elif frame.get('has_voice'):
            content = f"{Fore.GREEN}Voice Audio{Style.RESET_ALL}"
            
        print(f"{Fore.WHITE}[{timestamp}] #{fn:<4} {color}{ftype:<15}{Style.RESET_ALL} {enc} {content}")


def main():
    """Main entry point with CLI argument support."""
    import argparse
    
    def _parse_sdr_device(value):
        """Parse -d/--device: integer index or serial string.

        Plain small integers without leading zeros (0, 1, 2, …) are indexes.
        Values with leading zeros (e.g. 00000001) are treated as serials.
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        # Index: 0 or 1–999 without leading zeros
        if text == "0" or (text.isdigit() and not text.startswith("0") and len(text) <= 3):
            return int(text)
        return text

    parser = argparse.ArgumentParser(
        description='TETRA Decoder Pro - Modern GUI with live voice decoding',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s                              # Launch GUI normally
  %(prog)s --no-gui -f 392.225          # Run in CLI mode
  %(prog)s -f 392.225 --auto-start      # Auto-start capture
  %(prog)s -f 392.225 --auto-start -m   # Auto-start with audio monitoring
  %(prog)s -f 392.225 -g 35 -s 1.8      # Set frequency, gain, sample rate
  %(prog)s -d 1 --no-gui -f 392.225     # Use RTL-SDR device index 1
  %(prog)s -d 00000001 -f 392.225       # Use RTL-SDR by serial number
  %(prog)s --scan 390 392               # Scan range first, then launch GUI
  %(prog)s --debug                      # Verbose logs + per-frame JSONL
        '''
    )
    
    parser.add_argument('-f', '--frequency', type=float, default=390.865,
                        help='Frequency in MHz (default: 390.865)')
    parser.add_argument('-g', '--gain', type=float, default=50.0,
                        help='RF gain in dB (default: 50.0)')
    parser.add_argument('-s', '--sample-rate', type=float, default=2.4,
                        help='Sample rate in MHz (default: 2.4)')
    parser.add_argument('-d', '--device', type=_parse_sdr_device, default=None,
                        help='RTL-SDR device index (int) or serial number (str)')
    parser.add_argument('--auto-start', action='store_true',
                        help='Automatically start capture on launch')
    parser.add_argument('-m', '--monitor-audio', action='store_true',
                        help='Enable audio monitoring on start')
    parser.add_argument('--scan', nargs=2, type=float, metavar=('START', 'STOP'),
                        help='Scan frequency range first (in MHz)')
    parser.add_argument('--auto-decrypt', action='store_true', default=True,
                        help='Enable auto-decryption (default: enabled)')
    parser.add_argument('--debug', action='store_true',
                        help='Debug logging: DEBUG files + one JSONL line per decoded frame')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Same as --debug')
    parser.add_argument('--no-gui', action='store_true',
                        help='Run in CLI mode without GUI')
    
    args = parser.parse_args()

    debug = bool(args.debug or args.verbose)
    log_files = _setup_logging(debug=debug)
    logger.info("Logging to: %s%s", _get_log_dir(), " (debug)" if debug else "")
    if debug:
        logger.debug("Log files: %s", {k: str(v) for k, v in log_files.items()})
    
    # Scan if requested
    if args.scan:
        print("=" * 60)
        print("SCANNING FOR TETRA SIGNALS")
        print("=" * 60)
        from tetraear.signal.capture import RTLCapture
        from tetraear.signal.scanner import FrequencyScanner
        
        start_freq = args.scan[0] * 1e6
        stop_freq = args.scan[1] * 1e6
        
        rtl = RTLCapture(
            frequency=start_freq,
            sample_rate=2.4e6,
            gain=50,
            device=args.device,
        )
        if rtl.open():
            scanner = FrequencyScanner(
                rtl, 
                sample_rate=2.4e6,
                scan_step=25e3,
                noise_floor=-45,  # Default noise floor
                bottom_threshold=-85  # Default bottom threshold
            )
            results = []
            
            print(f"\nScanning {args.scan[0]:.3f} - {args.scan[1]:.3f} MHz...")
            freq = start_freq
            while freq <= stop_freq:
                result = scanner.scan_frequency(freq)
                if result['power_db'] > -60:
                    results.append(result)
                    print(f"  {freq/1e6:.3f} MHz: {result['power_db']:.1f} dB *** SIGNAL")
                freq += 25e3
            
            rtl.close()
            
            if results:
                results.sort(key=lambda x: x['power_db'], reverse=True)
                best = results[0]
                print(f"\n[OK] Best signal: {best['frequency']/1e6:.3f} MHz ({best['power_db']:.1f} dB)")
                if not args.frequency:
                    args.frequency = best['frequency'] / 1e6
                    print(f"  Auto-setting frequency to {args.frequency:.3f} MHz")
            else:
                print("\n[X] No strong signals found")
            print()
    
    if args.no_gui:
        import signal as _signal
        from PyQt6.QtCore import QCoreApplication, QTimer
        
        app = QCoreApplication(sys.argv)
        
        # Set UTF-8 encoding for Windows console
        if sys.platform == 'win32':
            import os
            os.system('chcp 65001 >nul 2>&1')
        
        print(f"{Fore.CYAN}TETRA Decoder Pro - CLI Mode{Style.RESET_ALL}")
        print(f"Frequency: {args.frequency} MHz")
        print(f"Gain: {args.gain} dB")
        print(f"Sample Rate: {args.sample_rate} MHz")
        if args.device is not None:
            print(f"SDR Device: {args.device}")
        print(f"{Fore.YELLOW}Ctrl+C to stop{Style.RESET_ALL}")
        
        capture_thread = CaptureThread()
        capture_thread.frequency = args.frequency * 1e6
        capture_thread.gain = args.gain
        capture_thread.sample_rate = args.sample_rate * 1e6
        capture_thread.sdr_device = args.device
        capture_thread.auto_decrypt = args.auto_decrypt
        
        listener = CLITetraListener(capture_thread)
        
        def _shutdown(*_args):
            print(f"\n{Fore.YELLOW}Stopping...{Style.RESET_ALL}")
            try:
                capture_thread.stop()
            except Exception:
                pass
            app.quit()

        # QCoreApplication.exec() does not raise KeyboardInterrupt on its own:
        # install SIGINT/SIGTERM and poke the event loop so Python can run handlers.
        _signal.signal(_signal.SIGINT, _shutdown)
        try:
            _signal.signal(_signal.SIGTERM, _shutdown)
        except (AttributeError, ValueError):
            pass
        _sig_timer = QTimer()
        _sig_timer.start(200)
        _sig_timer.timeout.connect(lambda: None)
        
        capture_thread.start()
        
        try:
            rc = app.exec()
        finally:
            try:
                capture_thread.stop()
                capture_thread.wait(3000)
            except Exception:
                pass
        sys.exit(rc)
            
    else:
        # Create Qt application
        app = QApplication(sys.argv)
        app.setStyle('Fusion')
        
        window = ModernTetraGUI()
        
        # Apply CLI arguments
        if args.device is not None:
            window.settings_manager.set("sdr_device", args.device)
            logger.info(f"Set SDR device to {args.device!r}")

        if args.frequency:
            window.freq_input.setText(f"{args.frequency:.3f}")
            logger.info(f"Set frequency to {args.frequency:.3f} MHz")
        
        if args.gain != 50.0:
            # Convert gain to slider value (0-100 = 0-50 dB)
            slider_val = int(args.gain * 2)
            window.gain_slider.setValue(slider_val)
            logger.info(f"Set gain to {args.gain:.1f} dB")
        
        if args.sample_rate != 2.4:
            # Convert sample rate to slider value (0 = 1.8 MHz, 6 = 2.4 MHz)
            # Formula: slider_value = (sample_rate - 1.8) / 0.1
            slider_val = int((args.sample_rate - 1.8) / 0.1)
            slider_val = max(0, min(6, slider_val))  # Clamp to valid range
            window.sample_rate_slider.setValue(slider_val)
            logger.info(f"Set sample rate to {args.sample_rate:.1f} MHz")
        
        if args.monitor_audio:
            window.hear_voice_cb.setChecked(True)
            logger.info("Audio monitoring enabled")
        
        if not args.auto_decrypt:
            window.auto_decrypt_cb.setChecked(False)
        
        window.show()
        
        # Auto-start if requested
        if args.auto_start:
            logger.info("Auto-starting capture...")
            # Use QTimer to start after GUI is fully initialized
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(500, window.on_start)
        
        sys.exit(app.exec())


if __name__ == '__main__':
    main()
