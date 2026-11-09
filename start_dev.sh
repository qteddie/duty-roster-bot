#!/usr/bin/env bash
# Start the React development server and the FastAPI API together.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$PROJECT_DIR/frontend"
PYTHON_BIN="${PYTHON_BIN:-}"
BACKEND_PID=""
FRONTEND_PID=""

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

cleanup() {
  trap - EXIT INT TERM
  [[ -n "$BACKEND_PID" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "$FRONTEND_PID" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
  [[ -n "$BACKEND_PID" ]] && wait "$BACKEND_PID" 2>/dev/null || true
  [[ -n "$FRONTEND_PID" ]] && wait "$FRONTEND_PID" 2>/dev/null || true
}

on_interrupt() {
  echo
  echo "正在關閉前後端…"
  exit 0
}

trap cleanup EXIT
trap on_interrupt INT TERM

command -v "$PYTHON_BIN" >/dev/null || {
  echo "找不到 Python：$PYTHON_BIN" >&2
  exit 1
}
command -v npm >/dev/null || {
  echo "找不到 npm。請先安裝 Node.js。" >&2
  exit 1
}

"$PYTHON_BIN" -c "import fastapi, uvicorn" 2>/dev/null || {
  echo "後端套件尚未安裝。請先執行：$PYTHON_BIN -m pip install -r requirements.txt" >&2
  exit 1
}

[[ -d "$FRONTEND_DIR/node_modules" ]] || {
  echo "前端套件尚未安裝。請先執行：cd frontend && npm install" >&2
  exit 1
}

for port in 8010 5173; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "連接埠 $port 已被使用。請先關閉既有服務後再執行此腳本。" >&2
    exit 1
  fi
done

cd "$PROJECT_DIR"
echo "啟動後端：http://127.0.0.1:8010"
"$PYTHON_BIN" -m uvicorn backend.app:app --host 127.0.0.1 --port 8010 --reload &
BACKEND_PID=$!

echo "啟動前端：http://127.0.0.1:5173"
(
  cd "$FRONTEND_DIR"
  npm run dev -- --host 127.0.0.1
) &
FRONTEND_PID=$!

echo
echo "開啟：http://127.0.0.1:5173"
echo "按 Ctrl+C 可同時停止前後端。"

while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done

echo "其中一個服務意外停止，正在關閉另一個服務。" >&2
exit 1
