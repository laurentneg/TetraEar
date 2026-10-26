#Requires -Version 5.1
<#
.SYNOPSIS
  Install TETRA codecs on Windows (ETSI exe + native DLLs).

.DESCRIPTION
  Wrapper around `python -m tetraear.tools.install_tetra_codec`.

  Builds into:
    tetraear\tetra_codec\bin\  — cdecoder.exe, sdecoder.exe (fallback)
    tetraear\tetra_codec\lib\  — tetra_cdec.dll, mbe-neo.dll (preferred)

  Prerequisites (MSYS2 MinGW64 recommended):
    pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake `
              mingw-w64-x86_64-make patch unzip git

  Then either:
    - Run this script from a "MSYS2 MinGW 64-bit" shell, OR
    - Add C:\msys64\mingw64\bin and C:\msys64\usr\bin to PATH before running.

.PARAMETER BuildDir
  Optional build directory (default: %TEMP%\tetra_codec_build).

.PARAMETER MsysRoot
  MSYS2 root (default: C:\msys64). Used to prepend toolchain to PATH.

.EXAMPLE
  .\scripts\install_tetra_codec.ps1

.EXAMPLE
  .\scripts\install_tetra_codec.ps1 -BuildDir D:\tmp\tetra_build
#>

[CmdletBinding()]
param(
    [string]$BuildDir = "",
    [string]$MsysRoot = "C:\msys64",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Add-PathFront([string]$Dir) {
    if ($Dir -and (Test-Path $Dir)) {
        if (-not (($env:PATH -split ";") -contains $Dir)) {
            $env:PATH = "$Dir;$env:PATH"
        }
    }
}

# Prefer MinGW64 toolchain for gcc/cmake/make/patch
Add-PathFront (Join-Path $MsysRoot "mingw64\bin")
Add-PathFront (Join-Path $MsysRoot "usr\bin")

$missing = @()
foreach ($tool in @("gcc", "cmake", "git", "patch")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        $missing += $tool
    }
}
$make = Get-Command "mingw32-make" -ErrorAction SilentlyContinue
if (-not $make) { $make = Get-Command "make" -ErrorAction SilentlyContinue }
if (-not $make) { $missing += "make/mingw32-make" }

if ($missing.Count -gt 0) {
    Write-Host "[ERROR] Missing tools: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "Install MSYS2 then:"
    Write-Host "  pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-make patch unzip git"
    Write-Host "Or pass -MsysRoot to your MSYS2 install path."
    exit 1
}

if (-not $Python) {
    $py = Get-Command "python" -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command "py" -ErrorAction SilentlyContinue }
    if (-not $py) {
        Write-Host "[ERROR] python not found in PATH" -ForegroundColor Red
        exit 1
    }
    $Python = $py.Source
}

Write-Host "====== TetraEar Windows codec install ======"
Write-Host "Root:    $Root"
Write-Host "Python:  $Python"
Write-Host "gcc:     $((Get-Command gcc).Source)"
Write-Host "cmake:   $((Get-Command cmake).Source)"

$argsList = @("-m", "tetraear.tools.install_tetra_codec")
if ($BuildDir) { $argsList += $BuildDir }

& $Python @argsList
exit $LASTEXITCODE
