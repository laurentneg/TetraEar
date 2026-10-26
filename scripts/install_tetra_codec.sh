#!/usr/bin/env bash
# Install ETSI TETRA codec executables AND native shared libraries:
#   - cdecoder/sdecoder under tetraear/tetra_codec/bin (subprocess fallback)
#   - libtetra_cdec.so (channel) + libmbe-neo.so (ACELP speech) under .../lib
#
# Build deps: curl, unzip, patch, make, gcc, cmake, git
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${1:-/tmp/tetra_codec_build}"
OSMO_DIR="${BUILD_DIR}/osmo-tetra"
ETSI_PATCH_DIR="${OSMO_DIR}/etsi_codec-patches"
CODEC_DIR="${OSMO_DIR}/codec"
CODEC_BIN_DIR="${ROOT_DIR}/tetraear/tetra_codec/bin"
CODEC_LIB_DIR="${ROOT_DIR}/tetraear/tetra_codec/lib"
CODEC_INC_DIR="${ROOT_DIR}/tetraear/tetra_codec/include"
NATIVE_DIR="${ROOT_DIR}/tetraear/tetra_codec/native"
VENDOR_DIR="${ROOT_DIR}/vendor"
MBELIB_DIR="${VENDOR_DIR}/mbelib-neo"
MBELIB_URL="${TETRAEAR_MBELIB_URL:-https://github.com/arancormonk/mbelib-neo.git}"

# HTTPS: plain HTTP often returns an HTML interstitial and breaks the MD5 check.
ETSI_URL="${TETRAEAR_ETSI_URL:-https://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip}"
ETSI_MD5="a8115fe68ef8f8cc466f4192572a1e3e"
ETSI_ZIP="${ETSI_PATCH_DIR}/etsi_tetra_codec.zip"

mkdir -p "${BUILD_DIR}"

if [ ! -d "${OSMO_DIR}" ]; then
  git clone https://gitea.osmocom.org/tetra/osmo-tetra "${OSMO_DIR}"
fi

if [ ! -f "${ETSI_ZIP}" ]; then
  if command -v curl >/dev/null 2>&1; then
    curl -L -A "Mozilla/5.0" "${ETSI_URL}" -o "${ETSI_ZIP}"
  else
    echo "curl is required to download ETSI codec (install curl or place ${ETSI_ZIP} manually)."
    exit 1
  fi
fi

if command -v md5sum >/dev/null 2>&1; then
  MD5_ACTUAL="$(md5sum "${ETSI_ZIP}" | awk '{print $1}')"
elif command -v md5 >/dev/null 2>&1; then
  MD5_ACTUAL="$(md5 -q "${ETSI_ZIP}")"
else
  MD5_ACTUAL=""
fi

if [ -n "${MD5_ACTUAL}" ] && [ "${MD5_ACTUAL}" != "${ETSI_MD5}" ]; then
  echo "ETSI codec MD5 mismatch: expected ${ETSI_MD5}, got ${MD5_ACTUAL}"
  echo "Hint: use HTTPS with a browser User-Agent, or set TETRAEAR_ETSI_URL to a local copy."
  exit 1
fi

rm -rf "${CODEC_DIR}"
mkdir -p "${CODEC_DIR}"
unzip -L "${ETSI_ZIP}" -d "${CODEC_DIR}" >/dev/null

while read -r patch_file; do
  [ -z "${patch_file}" ] && continue
  patch --batch -p1 -d "${CODEC_DIR}" < "${ETSI_PATCH_DIR}/${patch_file}"
done < "${ETSI_PATCH_DIR}/series"

make -C "${CODEC_DIR}/c-code"

mkdir -p "${CODEC_BIN_DIR}"
cp -f "${CODEC_DIR}/c-code/cdecoder" "${CODEC_DIR}/c-code/sdecoder" "${CODEC_BIN_DIR}/"
echo "Installed codec binaries to ${CODEC_BIN_DIR}"

# --- Native channel decoder (libtetra_cdec.so) ---
mkdir -p "${NATIVE_DIR}/etsi_cdec"
# Copy the patched channel-decoder sources + tables used by the façade.
for f in cdec_tet.c sub_cd.c tetra_op.c source.h channel.h; do
  cp -f "${CODEC_DIR}/c-code/${f}" "${NATIVE_DIR}/etsi_cdec/"
done
cp -f "${CODEC_DIR}/c-code/"*.tab "${NATIVE_DIR}/etsi_cdec/" 2>/dev/null || true
make -C "${NATIVE_DIR}" clean all
echo "Built ${CODEC_LIB_DIR}/libtetra_cdec.so"

# --- Speech decoder (mbelib-neo) ---
if [ ! -d "${MBELIB_DIR}/.git" ] && [ ! -f "${MBELIB_DIR}/CMakeLists.txt" ]; then
  mkdir -p "${VENDOR_DIR}"
  git clone --depth 1 "${MBELIB_URL}" "${MBELIB_DIR}"
fi
MBE_BUILD="${BUILD_DIR}/mbelib-neo-build"
cmake -S "${MBELIB_DIR}" -B "${MBE_BUILD}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=ON \
  -DMBELIB_BUILD_TESTS=OFF
cmake --build "${MBE_BUILD}" -j"$(nproc 2>/dev/null || echo 2)"
mkdir -p "${CODEC_LIB_DIR}" "${CODEC_INC_DIR}"
# Locate the built shared library (layout varies by CMake version)
MBE_SO="$(find "${MBE_BUILD}" -name 'libmbe-neo.so*' -type f | head -n1 || true)"
if [ -z "${MBE_SO}" ]; then
  MBE_SO="$(find "${MBE_BUILD}" -name 'libmbe-neo.so' | head -n1 || true)"
fi
if [ -z "${MBE_SO}" ]; then
  echo "mbelib-neo build did not produce libmbe-neo.so"
  exit 1
fi
cp -a "$(dirname "${MBE_SO}")"/libmbe-neo.so* "${CODEC_LIB_DIR}/"
cp -a "${MBELIB_DIR}/include/mbelib-neo" "${CODEC_INC_DIR}/"
echo "Installed mbelib-neo to ${CODEC_LIB_DIR}"

echo
echo "Native codec ready:"
echo "  ${CODEC_LIB_DIR}/libtetra_cdec.so"
ls -1 "${CODEC_LIB_DIR}"/libmbe-neo.so* 2>/dev/null | sed 's/^/  /'
echo "Python: VoiceProcessor prefers these libs; executables remain as fallback."
