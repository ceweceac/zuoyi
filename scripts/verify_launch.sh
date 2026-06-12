#!/usr/bin/env bash
# 隔离启动 qa-bot，专供 verify / 行为验证用。绝不碰生产（生产在 8080/8088）。
#
# 隔离手段（每一项都是为了不污染生产、且不触发 verify 的 destructive-path 护栏）：
#   - 端口 18080，不碰 8080 生产实例
#   - 临时 SQLite 库（/tmp），种子自动建 admin/auditor/editor，不碰生产库
#   - QABOT_DISABLE_BOT=1 → 完全不启动钉钉 Stream（不连真钉钉、不发任何消息，
#     也避免钉钉连接同步重试阻塞 ASGI 启动导致 /api/health 不通）
#   - QABOT_ALLOW_WEAK_JWT=1 + 临时 JWT_SECRET（临时库无所谓）
#   - LLM_ENABLED=false → 不调用真 LLM
#
# 注意：本脚本只用环境变量隔离，不改动 backend/.env（生产凭证文件原样不动）。
#       pydantic-settings 中环境变量优先级高于 .env 文件，DATABASE_URL/JWT_SECRET 均生效。
#
# 用法：
#   scripts/verify_launch.sh up      # 起实例，等 /api/health 通，打印 VERIFY_READY
#   scripts/verify_launch.sh down    # 停实例 + 删临时库
set -uo pipefail

PORT=18080
PIDFILE=/tmp/qabot_verify.pid
DBFILE=/tmp/qabot_verify.db
LOGFILE=/tmp/qabot_verify.log
HERE="$(cd "$(dirname "$0")/../backend" && pwd)"
PY="$HERE/.venv/bin/python"

up() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "已在运行 PID=$(cat "$PIDFILE")"; exit 0
  fi
  rm -f "$DBFILE"
  cd "$HERE" || exit 1

  QABOT_DISABLE_BOT=1 \
  QABOT_ALLOW_WEAK_JWT=1 \
  QABOT_SKIP_CHARCHECK=1 \
  QABOT_UI_STORAGE_SECRET=verify-throwaway-storage-secret \
  JWT_SECRET=verify-only-throwaway-secret-not-for-production-32bytes \
  DATABASE_URL="sqlite:///$DBFILE" \
  LLM_ENABLED=false \
  nohup "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
    > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  echo "启动中 PID=$(cat "$PIDFILE") 日志=$LOGFILE"
  for i in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/health" 2>/dev/null)
    if [ "$code" = "200" ]; then
      echo "VERIFY_READY http://127.0.0.1:$PORT"
      echo "种子账号: admin/admin123  auditor/auditor123  editor/editor123"
      exit 0
    fi
    sleep 0.5
  done
  echo "BLOCKED: 30s 内 /api/health 未通，看日志 $LOGFILE"
  tail -20 "$LOGFILE"
  exit 1
}

down() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
    echo "已停止"
  else
    pkill -f "uvicorn app.main:app --host 127.0.0.1 --port $PORT" 2>/dev/null && echo "已按端口停止" || echo "未在运行"
  fi
  rm -f "$DBFILE"
  echo "已删临时库"
}

case "${1:-}" in
  up)   up ;;
  down) down ;;
  *)    echo "用法: $0 {up|down}"; exit 2 ;;
esac
