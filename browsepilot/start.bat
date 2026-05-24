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
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9400 :8000 :5173" ^| findstr "LISTENING"') do (
    taskkill /pid %%a /f >nul 2>&1
)
echo.

:: ---- MCP Pool (Playwright MCP on port 9400) ----
echo [1/3] Starting MCP Pool ^(Playwright MCP^)...
start "BrowsePilot-MCP" cmd /k ".venv\Scripts\python.exe -m browser_mcp.main"
echo   MCP Pool started

timeout /t 3 /nobreak >nul

:: ---- Backend (Port 8000) ----
echo [2/3] Starting Backend...
start "BrowsePilot-Backend" cmd /k ".venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000"
echo   Backend started

timeout /t 2 /nobreak >nul

:: ---- Vue3 Frontend (Port 5173) ----
echo [3/3] Starting Vue3 Frontend...
start "BrowsePilot-Frontend" cmd /k "cd frontend-vue && npm run dev"
echo   Frontend started

echo.
echo ==========================================
echo   Backend:  http://localhost:8000
echo   Frontend: http://localhost:5173
echo   (MCP pool on port 9400, no HTTP UI)
echo ==========================================
echo.
echo Close each window to stop the service.
pause

endlocal
