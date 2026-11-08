@echo off
REM ============================================================
REM  ZhiXiao - one-shot local setup (Windows)
REM  Creates .venv, installs deps, creates tables, seeds demo users.
REM  Run from anywhere:  scripts\setup.bat
REM ============================================================
setlocal
cd /d "%~dp0.."

set PY=python
where python >nul 2>nul || (
  echo [ERROR] python not found in PATH. Install Python 3.11+ first.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/5] creating venv .venv ...
  %PY% -m venv .venv || exit /b 1
) else (
  echo [1/5] venv already exists, skip
)

echo [2/5] installing dependencies ...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || exit /b 1

if not exist ".env" (
  echo [3/5] .env missing, copying from .env.example ...
  copy /y ".env.example" ".env" >nul
  echo       NOTE: edit .env and replace JWT_SECRET before real use.
) else (
  echo [3/5] .env exists, skip
)

echo [4/5] creating tables ...
".venv\Scripts\python.exe" tools\init_db.py || exit /b 1

echo [5/5] seeding demo users ...
".venv\Scripts\python.exe" tools\seed_demo.py || exit /b 1

echo.
echo [OK] setup finished. Next:
echo      scripts\start-backend.bat      # http://127.0.0.1:5000/api/v1/health
echo      scripts\start-frontend.bat     # http://127.0.0.1:5173
endlocal
