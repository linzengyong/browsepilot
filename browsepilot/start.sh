#!/usr/bin/env bash
# BrowsePilot — 一键启动所有服务 (Backend + Frontend)
# Usage: bash start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtualenv if present
if [ -f ".venv/Scripts/activate" ]; then
    source .venv/Scripts/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Clean up background processes on exit
cleanup() {
    echo ""
    echo "正在停止所有服务..."
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
    wait $BACKEND_PID $FRONTEND_PID 2>/dev/null
    echo "所有服务已停止"
}
trap cleanup EXIT INT TERM

echo "=========================================="
echo "  BrowsePilot — 启动所有服务"
echo "=========================================="

# ---- Backend (Port 8001) ----
echo "[1/2] 启动 Backend (port 8001)..."
uv run uvicorn browsepilot.app.main:app --host 127.0.0.1 --port 8001 &
BACKEND_PID=$!
sleep 2
echo "  Backend PID: $BACKEND_PID"

# ---- Vue3 Frontend (Port 5173) ----
echo "[2/2] 启动 Frontend (port 5173)..."
(cd frontend-vue && npm run dev) &
FRONTEND_PID=$!
sleep 3
echo "  Frontend PID: $FRONTEND_PID"

echo ""
echo "=========================================="
echo "  所有服务已启动"
echo "  Backend:  http://localhost:8001"
echo "  Frontend: http://localhost:5173"
echo "=========================================="
echo ""
echo "按 Ctrl+C 停止所有服务"

# Wait for all background processes
wait
