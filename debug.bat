@echo off
chcp 65001 >nul
set "HERE=%~dp0"
set "PY=C:\Users\m-win\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%HERE%"
echo Starting Air Mouse Key Mapper ...
echo GUI: http://127.0.0.1:8765/
"%PY%" remapper.py
echo.
echo Engine stopped. Press any key to close.
pause >nul
