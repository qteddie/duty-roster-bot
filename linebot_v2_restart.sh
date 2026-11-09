#!/usr/bin/env bash
# Rebuild and restart the V2 全員班表 LINE bot (and the shared Cloudflare Tunnel).
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if [[ ! -f .env ]]; then
  echo "找不到 .env。請先由 .env.example 建立，並填入 LINE_V2_* 與 Cloudflare Tunnel 設定。" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "找不到 Docker Compose。請先安裝並啟動 Docker Engine。" >&2
  exit 1
fi

required_vars=(
  LINE_V2_CHANNEL_ACCESS_TOKEN
  LINE_V2_CHANNEL_SECRET
  LINE_V2_PUBLIC_BASE_URL
  CLOUDFLARE_TUNNEL_TOKEN
)
for variable_name in "${required_vars[@]}"; do
  if ! grep -Eq "^${variable_name}=.+$" .env || grep -Eq "^${variable_name}=replace_with_" .env; then
    echo ".env 的 ${variable_name} 尚未設定。" >&2
    exit 1
  fi
done
if ! grep -Eq "^LINE_V2_PUBLIC_BASE_URL=https://" .env; then
  echo "LINE_V2_PUBLIC_BASE_URL 必須是 https 網址（LINE 只接受 https 圖片）。" >&2
  exit 1
fi

model="$(grep -E '^LINE_V2_RECOGNIZER=' .env | cut -d= -f2- || true)"
if [[ -n "$model" ]]; then
  local_model="${model#/app/}"
  [[ -f "$local_model" ]] || { echo "找不到模型 $local_model（LINE_V2_RECOGNIZER）。" >&2; exit 1; }
elif ! ls models/all_staff/line-*/recognizer.pt >/dev/null 2>&1; then
  echo "找不到 models/all_staff/line-*/recognizer.pt。模型不在 git 裡，請先從訓練機複製過來。" >&2
  exit 1
fi

mkdir -p data
docker compose up --detach --build --force-recreate work-schedule-line-v2 cloudflared
docker compose ps work-schedule-line-v2 cloudflared
echo
echo "看記錄：docker compose logs --follow work-schedule-line-v2"
