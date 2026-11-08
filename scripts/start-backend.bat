@echo off
REM Start backend on http://127.0.0.1:5000
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run scripts\setup.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" server.py %*
endlocal
