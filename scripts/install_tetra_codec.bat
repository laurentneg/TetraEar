@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Install TETRA codecs on Windows (ETSI exe + native DLLs).
REM Wrapper around: python -m tetraear.tools.install_tetra_codec
REM
REM Usage:
REM   scripts\install_tetra_codec.bat
REM   scripts\install_tetra_codec.bat D:\tmp\tetra_build
REM   scripts\install_tetra_codec.bat D:\tmp\tetra_build C:\msys64
REM
REM Prerequisites (MSYS2 MinGW64):
REM   pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-make patch unzip git

set "ROOT=%~dp0.."
pushd "%ROOT%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot cd to project root: %ROOT%
  exit /b 1
)

set "BUILD_DIR=%~1"
set "MSYS_ROOT=%~2"
if "%MSYS_ROOT%"=="" set "MSYS_ROOT=C:\msys64"

if exist "%MSYS_ROOT%\mingw64\bin" set "PATH=%MSYS_ROOT%\mingw64\bin;%PATH%"
if exist "%MSYS_ROOT%\usr\bin" set "PATH=%MSYS_ROOT%\usr\bin;%PATH%"

set "MISSING="
where gcc >nul 2>&1 || set "MISSING=!MISSING! gcc"
where cmake >nul 2>&1 || set "MISSING=!MISSING! cmake"
where git >nul 2>&1 || set "MISSING=!MISSING! git"
where patch >nul 2>&1 || set "MISSING=!MISSING! patch"
where mingw32-make >nul 2>&1
if errorlevel 1 (
  where make >nul 2>&1 || set "MISSING=!MISSING! make/mingw32-make"
)

if not "!MISSING!"=="" (
  echo [ERROR] Missing tools:!MISSING!
  echo Install MSYS2 then:
  echo   pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-make patch unzip git
  echo Or pass MSYS root as 2nd argument, e.g.:
  echo   %~nx0 "" D:\msys64
  popd >nul
  exit /b 1
)

set "PYTHON="
where python >nul 2>&1 && set "PYTHON=python"
if "%PYTHON%"=="" (
  where py >nul 2>&1 && set "PYTHON=py -3"
)
if "%PYTHON%"=="" (
  echo [ERROR] python not found in PATH
  popd >nul
  exit /b 1
)

echo ====== TetraEar Windows codec install ======
echo Root:    %CD%
echo Python:  %PYTHON%
where gcc
where cmake
echo.

if "%BUILD_DIR%"=="" (
  %PYTHON% -m tetraear.tools.install_tetra_codec
) else (
  %PYTHON% -m tetraear.tools.install_tetra_codec "%BUILD_DIR%"
)
set "RC=%ERRORLEVEL%"
popd >nul
exit /b %RC%
