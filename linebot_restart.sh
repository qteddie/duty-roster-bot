#!/usr/bin/env bash
# Rebuild and restart only the LINE bot service.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if [[ ! -f .env ]]; then
  echo "找不到 .env。請先由 .env.example 建立，並填入 LINE 與 Cloudflare Tunnel 設定。" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "找不到 Docker Compose。請先安裝並啟動 Docker Engine。" >&2
  exit 1
fi

required_vars=(
  OPENAI_API_KEY
  LINE_CHANNEL_ACCESS_TOKEN
  LINE_CHANNEL_SECRET
  LINE_PUBLIC_BASE_URL
  CLOUDFLARE_TUNNEL_TOKEN
)

for variable_name in "${required_vars[@]}"; do
  if ! grep -Eq "^${variable_name}=.+$" .env || grep -Eq "^${variable_name}=replace_with_" .env; then
    echo ".env 的 ${variable_name} 尚未設定。" >&2
    exit 1
  fi
done

docker compose up --detach --build --force-recreate work-schedule-line cloudflared
docker compose ps work-schedule-line cloudflared
