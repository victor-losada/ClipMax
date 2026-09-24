@echo off
REM ClipMax - demo con datos sinteticos. Se ve en http://127.0.0.1:5001 (no toca tus datos reales).
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Primero ejecuta instalar.bat
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python arrancar.py demo --ver
pause
