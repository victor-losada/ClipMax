@echo off
REM ============================================================
REM  ClipMax - instalacion en Windows (ejecutar una sola vez)
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] No encuentro Python. Instalalo desde https://www.python.org/downloads/ ^(3.12^)
  echo         y marca "Add python.exe to PATH" durante la instalacion.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creando entorno virtual...
  py -3 -m venv .venv || (echo [ERROR] no se pudo crear .venv & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || (echo [ERROR] fallo pip install & pause & exit /b 1)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo ffmpeg no esta en el PATH. Intentando instalarlo con winget...
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (
    echo winget no disponible: descargo ffmpeg dentro de bin\
    python arrancar.py descargar --sin-whisper --modelo --ffmpeg
  )
)

echo.
echo Descargando whisper.cpp y modelos base + small ^(~650 MB^)...
python arrancar.py descargar --modelo base small

if not exist config.yaml copy config.example.yaml config.yaml >nul
if not exist .env copy .env.example .env >nul

echo.
echo ============================================================
echo  Listo. Pasos siguientes:
echo   1. Abre .env y pega tu ANTHROPIC_API_KEY ^(o usa modo manual^)
echo   2. Ejecuta iniciar.bat y configura streamers en el navegador
echo   3. Si instalaste ffmpeg con winget, cierra y abre la consola
echo ============================================================
python arrancar.py doctor --sin-red
pause
