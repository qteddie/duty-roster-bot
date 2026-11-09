#!/usr/bin/env bash
# Rebuild and restart the Discord bot with Docker Compose.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if ! command -v docker >/dev/null 2>&1; then
  echo "找不到 Docker。請先在 WSL 安裝並啟動 Docker Engine。" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "找不到 Docker Compose plugin。請安裝 docker-compose-plugin。" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "找不到 .env。請先執行：cp .env.example .env，並填入 API 與 Discord token。" >&2
  exit 1
fi

mkdir -p data
docker compose up --detach --build --force-recreate --remove-orphans work-schedule-discord
docker compose ps work-schedule-discord
