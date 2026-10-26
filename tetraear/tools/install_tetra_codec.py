#!/usr/bin/env python3
"""
TETRA codec installer (Windows / MSYS2 MinGW, also usable on Linux).

Builds:
  - cdecoder / sdecoder (.exe on Windows) → tetraear/tetra_codec/bin/
  - tetra_cdec shared lib (.dll / .so)     → tetraear/tetra_codec/lib/
  - mbelib-neo (mbe-neo.dll / libmbe-neo.so)

Requirements (Windows — MSYS2 MinGW64 shell recommended):
  pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake \\
            mingw-w64-x86_64-make patch unzip git

  Or from PowerShell after adding MinGW64\\bin to PATH:
    python -m tetraear.tools.install_tetra_codec
    # or: .\\scripts\\install_tetra_codec.ps1

Usage:
  python -m tetraear.tools.install_tetra_codec [build_dir]
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# ETSI / corp proxies sometimes MITM; override with TETRAEAR_ETSI_ZIP if needed.
ssl._create_default_https_context = ssl._create_unverified_context

SCRIPT_VERSION = "2.0"

ETSI_URL = os.environ.get(
    "TETRAEAR_ETSI_URL",
    "https://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/"
    "en_30039502v010301p0.zip",
)
ETSI_MD5 = "a8115fe68ef8f8cc466f4192572a1e3e"
MBELIB_URL = os.environ.get(
    "TETRAEAR_MBELIB_URL",
    "https://github.com/arancormonk/mbelib-neo.git",
)
OSMO_URL = "https://gitea.osmocom.org/tetra/osmo-tetra"

TETRAEAR_PKG = Path(__file__).resolve().parents[1]
ROOT_DIR = TETRAEAR_PKG.parent
CODEC_BIN_DIR = TETRAEAR_PKG / "tetra_codec" / "bin"
CODEC_LIB_DIR = TETRAEAR_PKG / "tetra_codec" / "lib"
CODEC_INC_DIR = TETRAEAR_PKG / "tetra_codec" / "include"
NATIVE_DIR = TETRAEAR_PKG / "tetra_codec" / "native"
VENDOR_DIR = ROOT_DIR / "vendor"
MBELIB_DIR = VENDOR_DIR / "mbelib-neo"

IS_WINDOWS = os.name == "nt"


def fail(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], *, cwd: Path | str | None = None) -> None:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.check_call([str(c) for c in cmd], cwd=str(cwd) if cwd else None)


def which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def find_make() -> str:
    path = which("mingw32-make", "make")
    if not path:
        fail("Neither make nor mingw32-make found in PATH")
    return path


def find_gcc() -> str:
    path = which("gcc")
    if not path:
        fail("gcc not found in PATH (install MSYS2 mingw-w64-x86_64-gcc)")
    return path


def find_cmake() -> str:
    path = which("cmake")
    if not path:
        fail("cmake not found in PATH (install mingw-w64-x86_64-cmake)")
    return path


def check_prerequisites() -> None:
    print("[*] Checking toolchain...")
    find_gcc()
    find_make()
    find_cmake()
    if which("git") is None:
        fail("git not found in PATH")
    if which("patch") is None:
        fail("patch not found in PATH (MSYS2: pacman -S patch)")
    print("[+] Tools OK")


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"[*] Using existing archive: {dest}")
        return
    print(f"[*] Downloading {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 TetraEar-codec-installer"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
    except Exception as e:
        fail(
            f"Download failed: {e}\n"
            "Hint: set TETRAEAR_ETSI_ZIP to a local copy of the ETSI zip."
        )


def verify_md5(path: Path, expected: str) -> None:
    print("[*] Verifying MD5...")
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            md5.update(chunk)
    digest = md5.hexdigest()
    if digest != expected:
        path.unlink(missing_ok=True)
        fail(f"MD5 mismatch: got {digest}, expected {expected}")
    print("[+] Checksum OK")


def normalize_line_endings(root: Path) -> None:
    print("[*] Normalizing line endings (CRLF → LF)...")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".c", ".h", ".tab"} and path.name.lower() not in {
            "makefile",
            "series",
        }:
            continue
        try:
            data = path.read_bytes()
            if b"\r\n" in data:
                path.write_bytes(data.replace(b"\r\n", b"\n"))
        except OSError:
            pass


def fix_makefiles(root: Path) -> None:
    print("[*] Adjusting Makefiles for modern GCC...")
    for path in root.rglob("*"):
        if path.name.lower() != "makefile":
            continue
        data = path.read_text(encoding="utf-8", errors="ignore")
        data = re.sub(r"(?m)^ACC\s*=\s*acc\b", "ACC = gcc", data)
        data = re.sub(r"(?m)^(\s*)acc\b", r"\1gcc", data)
        data = re.sub(r"\bacc\b", "gcc", data)
        if "-fcommon" not in data:
            data = re.sub(r"(?m)^CFLAGS\s*=\s*(.*)$", r"CFLAGS = -fcommon \1", data)
        data = data.replace("-Werror", "")
        path.write_text(data, encoding="utf-8")


def ensure_osmo(build_dir: Path) -> Path:
    osmo = build_dir / "osmo-tetra"
    if not osmo.is_dir():
        print(f"[*] Cloning osmo-tetra → {osmo}")
        run(["git", "clone", "--depth", "1", OSMO_URL, str(osmo)])
    return osmo


def prepare_etsi_sources(osmo: Path, etsi_zip: Path) -> Path:
    patch_dir = osmo / "etsi_codec-patches"
    codec_dir = osmo / "codec"
    if codec_dir.exists():
        shutil.rmtree(codec_dir)
    codec_dir.mkdir(parents=True)

    print(f"[*] Extracting ETSI sources → {codec_dir}")
    with zipfile.ZipFile(etsi_zip, "r") as zf:
        zf.extractall(codec_dir)

    series = patch_dir / "series"
    if not series.is_file():
        fail(f"Missing patch series: {series}")

    print("[*] Applying osmo-tetra patches...")
    for line in series.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patch_file = patch_dir / line
        if not patch_file.is_file():
            fail(f"Missing patch: {patch_file}")
        print(f"  patch {line}")
        with open(patch_file, "rb") as pf:
            subprocess.check_call(
                ["patch", "--batch", "-p1", "-d", str(codec_dir)],
                stdin=pf,
            )

    normalize_line_endings(codec_dir)
    fix_makefiles(codec_dir)
    return codec_dir


def find_c_code(codec_dir: Path) -> Path:
    for candidate in codec_dir.rglob("*"):
        if candidate.is_dir() and candidate.name.lower() == "c-code":
            return candidate
    fail("C-CODE directory not found after extract/patch")


def compile_exes(c_code: Path) -> None:
    make_cmd = find_make()
    makefile = None
    for f in c_code.iterdir():
        if f.name.lower() == "makefile":
            makefile = f.name
            break
    if not makefile:
        fail("Makefile not found in c-code")

    print(f"[*] Compiling ETSI executables in {c_code}")
    run([make_cmd, "-f", makefile], cwd=c_code)


def install_exes(c_code: Path) -> None:
    CODEC_BIN_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[*] Installing executables → {CODEC_BIN_DIR}")
    wanted = ("cdecoder", "sdecoder", "ccoder", "scoder")
    found: dict[str, Path] = {}
    for f in c_code.iterdir():
        name = f.name.lower()
        base = name[:-4] if name.endswith(".exe") else name
        if base in wanted and f.is_file() and os.access(f, os.X_OK | os.R_OK):
            # Prefer .exe on Windows when both exist
            if base in found and IS_WINDOWS and not name.endswith(".exe"):
                continue
            found[base] = f

    # Fallback: also accept non-executable object names on some FS
    if "cdecoder" not in found or "sdecoder" not in found:
        for f in c_code.iterdir():
            name = f.name.lower()
            base = name[:-4] if name.endswith(".exe") else name
            if base in wanted and f.is_file():
                found.setdefault(base, f)

    for base in ("cdecoder", "sdecoder"):
        if base not in found:
            fail(f"Missing binary after build: {base}")

    for base, src in found.items():
        dst_name = f"{base}.exe" if IS_WINDOWS else base
        dst = CODEC_BIN_DIR / dst_name
        shutil.copy2(src, dst)
        print(f"  + {dst.name}")


def _resolve_src(c_code: Path, name: str) -> Path:
    direct = c_code / name
    if direct.is_file():
        return direct
    matches = list(c_code.glob(name)) + [
        p for p in c_code.iterdir() if p.name.lower() == name.lower()
    ]
    if not matches:
        fail(f"Missing {name} in {c_code}")
    return matches[0]


def sync_native_sources(c_code: Path) -> None:
    etsi_dst = NATIVE_DIR / "etsi_cdec"
    etsi_dst.mkdir(parents=True, exist_ok=True)
    for name in ("cdec_tet.c", "sub_cd.c", "tetra_op.c", "source.h", "channel.h"):
        src = _resolve_src(c_code, name)
        shutil.copy2(src, etsi_dst / name)
    for tab in c_code.glob("*.tab"):
        shutil.copy2(tab, etsi_dst / tab.name)
    # Also copy case-insensitive .TAB
    for tab in c_code.iterdir():
        if tab.suffix.lower() == ".tab" and tab.is_file():
            shutil.copy2(tab, etsi_dst / tab.name.lower())
    print(f"[*] Synced channel-decoder sources → {etsi_dst}")


def build_tetra_cdec_shared() -> Path:
    gcc = find_gcc()
    CODEC_LIB_DIR.mkdir(parents=True, exist_ok=True)
    srcs = [
        NATIVE_DIR / "tetra_cdec.c",
        NATIVE_DIR / "etsi_cdec" / "cdec_tet.c",
        NATIVE_DIR / "etsi_cdec" / "sub_cd.c",
        NATIVE_DIR / "etsi_cdec" / "tetra_op.c",
    ]
    for s in srcs:
        if not s.is_file():
            fail(f"Missing native source: {s}")

    includes = [f"-I{NATIVE_DIR}", f"-I{NATIVE_DIR / 'etsi_cdec'}"]
    if IS_WINDOWS:
        out = CODEC_LIB_DIR / "tetra_cdec.dll"
        cmd = [
            gcc,
            "-O3",
            "-shared",
            "-fPIC",
            "-DTETRA_CDEC_BUILDING",
            *includes,
            "-o",
            str(out),
            *[str(s) for s in srcs],
            f"-Wl,--out-implib,{CODEC_LIB_DIR / 'libtetra_cdec.dll.a'}",
        ]
    else:
        out = CODEC_LIB_DIR / "libtetra_cdec.so"
        cmd = [
            gcc,
            "-O3",
            "-shared",
            "-fPIC",
            *includes,
            "-o",
            str(out),
            *[str(s) for s in srcs],
        ]

    print(f"[*] Building {out.name}...")
    run(cmd)
    if IS_WINDOWS:
        alias = CODEC_LIB_DIR / "libtetra_cdec.dll"
        shutil.copy2(out, alias)
        print(f"  + {alias.name}")
    print(f"  + {out.name}")
    return out


def ensure_mbelib() -> None:
    if (MBELIB_DIR / "CMakeLists.txt").is_file():
        print(f"[*] Using existing mbelib-neo at {MBELIB_DIR}")
        return
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[*] Cloning mbelib-neo → {MBELIB_DIR}")
    run(["git", "clone", "--depth", "1", MBELIB_URL, str(MBELIB_DIR)])


def build_mbelib(build_dir: Path) -> None:
    ensure_mbelib()
    cmake = find_cmake()
    mbe_build = build_dir / "mbelib-neo-build"
    if mbe_build.exists():
        shutil.rmtree(mbe_build)
    mbe_build.mkdir(parents=True)

    cfg = [
        cmake,
        "-S",
        str(MBELIB_DIR),
        "-B",
        str(mbe_build),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=ON",
        "-DMBELIB_BUILD_TESTS=OFF",
        "-DMBELIB_BUILD_EXAMPLES=OFF",
        "-DMBELIB_BUILD_TOOLS=OFF",
        "-DMBELIB_WARNINGS_AS_ERRORS=OFF",
    ]
    if IS_WINDOWS and which("ninja") is None:
        cfg[1:1] = ["-G", "MinGW Makefiles"]

    print("[*] Configuring mbelib-neo...")
    run(cfg)

    jobs = str(os.cpu_count() or 2)
    print("[*] Building mbelib-neo...")
    run([cmake, "--build", str(mbe_build), "--config", "Release", "-j", jobs])

    CODEC_LIB_DIR.mkdir(parents=True, exist_ok=True)
    CODEC_INC_DIR.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    patterns = ("mbe-neo.dll", "libmbe-neo.dll", "libmbe-neo.so", "mbe-neo.lib")
    for path in mbe_build.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name in patterns or name.startswith("libmbe-neo.so"):
            dst = CODEC_LIB_DIR / name
            shutil.copy2(path, dst)
            copied.append(name)
        elif IS_WINDOWS and name.lower().endswith(".dll") and "mbe" in name.lower():
            dst = CODEC_LIB_DIR / name
            shutil.copy2(path, dst)
            copied.append(name)

    if not any(n.endswith(".dll") or ".so" in n for n in copied):
        fail("mbelib-neo build did not produce a shared library")

    hdr_src = MBELIB_DIR / "include" / "mbelib-neo"
    hdr_dst = CODEC_INC_DIR / "mbelib-neo"
    if hdr_dst.exists():
        shutil.rmtree(hdr_dst)
    shutil.copytree(hdr_src, hdr_dst)
    print(f"[+] mbelib-neo installed: {', '.join(sorted(set(copied)))}")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    print(f"====== TETRA Codec Installer v{SCRIPT_VERSION} ======")
    print(f"Platform: {'Windows' if IS_WINDOWS else 'POSIX'}")

    default_build = Path(tempfile.gettempdir()) / "tetra_codec_build"
    build_dir = Path(argv[0]).expanduser().resolve() if argv else default_build
    build_dir.mkdir(parents=True, exist_ok=True)
    print(f"Build dir: {build_dir}")

    check_prerequisites()

    osmo = ensure_osmo(build_dir)
    etsi_zip = osmo / "etsi_codec-patches" / "etsi_tetra_codec.zip"
    local = os.environ.get("TETRAEAR_ETSI_ZIP")
    if local and Path(local).is_file():
        etsi_zip.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, etsi_zip)
        print(f"[*] Using TETRAEAR_ETSI_ZIP={local}")
    else:
        download(ETSI_URL, etsi_zip)
    verify_md5(etsi_zip, ETSI_MD5)

    codec_dir = prepare_etsi_sources(osmo, etsi_zip)
    c_code = find_c_code(codec_dir)
    compile_exes(c_code)
    install_exes(c_code)
    sync_native_sources(c_code)
    build_tetra_cdec_shared()
    build_mbelib(build_dir)

    print()
    print("[SUCCESS] Codec install complete:")
    print(f"  bin: {CODEC_BIN_DIR}")
    print(f"  lib: {CODEC_LIB_DIR}")
    print("  VoiceProcessor prefers native libs; executables remain as fallback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
