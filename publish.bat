@echo off
REM ============================================================================
REM Publish testhide-pytest-plugin to PyPI.
REM Reads tokens/env from .env.local (gitignored). Copy .env.local.example first.
REM Usage:  publish.bat
REM ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist ".env.local" (
  echo [publish] ERROR: .env.local not found.
  echo [publish] Copy .env.local.example to .env.local and add your PyPI token.
  exit /b 1
)

for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env.local") do set "%%a=%%b"

if "%PYPI_API_TOKEN%"=="" (
  echo [publish] ERROR: PYPI_API_TOKEN is not set in .env.local
  exit /b 1
)

echo [publish] Installing build tools...
python -m pip install build twine >nul || exit /b 1

echo [publish] Building sdist + wheel...
if exist dist rmdir /s /q dist
python -m build || exit /b 1

echo [publish] Uploading to PyPI...
set "TWINE_USERNAME=__token__"
set "TWINE_PASSWORD=%PYPI_API_TOKEN%"
python -m twine upload dist\* || exit /b 1

echo [publish] Done.
endlocal
