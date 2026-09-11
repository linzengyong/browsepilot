@echo off
chcp 65001 >nul
:: BrowsePilot — 一键启动所有服务

setlocal
cd /d "%~dp0"

echo ==========================================
echo   BrowsePilot
echo ==========================================
echo.

:: ---- Check venv ----
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found, run: uv sync
    pause
    exit /b 1
)

:: ---- Check frontend-vue ----
if not exist "frontend-vue\node_modules" (
    echo [ERROR] frontend-vue dependencies not installed, run: cd frontend-vue ^&^& npm install
    pause
    exit /b 1
)

:: ---- Clean ports ----
echo Cleaning up old processes...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001 :5173" ^| findstr "LISTENING"') do (
    taskkill /pid %%a /f >nul 2>&1
)
echo.

:: ---- Backend (Port 8001) ----
echo [1/2] Starting Backend...
start "BrowsePilot-Backend" cmd /k ".venv\Scripts\python.exe -m uvicorn browsepilot.app.main:app --host 127.0.0.1 --port 8001"
echo   Backend started

timeout /t 2 /nobreak >nul

:: ---- Vue3 Frontend (Port 5173) ----
echo [2/2] Starting Vue3 Frontend...
start "BrowsePilot-Frontend" cmd /k "cd frontend-vue && npm run dev"
echo   Frontend started

echo.
echo ==========================================
echo   Backend:  http://localhost:8001
echo   Frontend: http://localhost:5173
echo ==========================================
echo.
echo Close each window to stop the service.
pause

endlocal
