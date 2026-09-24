@echo off
REM ClipMax - abre la interfaz web y deja corriendo la grabacion automatica.
REM Deja esta ventana abierta durante el evento (puedes minimizarla).
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Primero ejecuta instalar.bat
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python arrancar.py web
pause
