@echo off
REM Start frontend dev server on http://127.0.0.1:5173 (proxies /api to :5000)
setlocal
cd /d "%~dp0..\frontend"
if not exist "node_modules" (
  echo [1/2] installing frontend dependencies ...
  call npm install
)
echo [2/2] starting vite ...
call npm run dev
endlocal
