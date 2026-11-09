@echo off
chcp 65001 >nul
set "HERE=%~dp0"
set "PY=C:\Users\m-win\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
if not exist "%PY%" set "PY=pythonw.exe"
cd /d "%HERE%"
if not exist "remapper.py" (
  echo [ERROR] remapper.py not found in %HERE%
  echo.
  pause
  exit /b 1
)
echo Starting Air Mouse Key Mapper engine ...
start "" "%PY%" "remapper.py"
echo.
echo The browser will open http://127.0.0.1:8765/ in a moment.
echo If it does not, open that address manually.
echo.
echo Having trouble? Run debug.bat instead - it shows the engine log.
echo.
timeout /t 3 >nul
