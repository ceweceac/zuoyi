#!/usr/bin/env bash
# A1 跑批进度监控
PID=$(cat /tmp/gen_qa_tags.pid 2>/dev/null)
LOG=/tmp/gen_qa_tags.log

if [ -z "$PID" ] || ! ps -p "$PID" >/dev/null 2>&1; then
  echo "❌ A1 没在跑"
  echo "   最后 10 行日志："
  tail -10 "$LOG" 2>/dev/null
  echo
  echo "   Excel 产物："
  ls -la /Users/dianzhong/Desktop/qa-bot/backend/data/tags_review.xlsx 2>/dev/null \
    || echo "   尚未生成"
  exit 0
fi

echo "✅ A1 在跑   PID=$PID"
ps -p "$PID" -o etime,pcpu,rss --no-headers 2>/dev/null | awk '{print "   已运行 " $1 "  CPU " $2 "%  内存 " int($3/1024) "MB"}'

# 解析进度
CURRENT=$(grep -oE '\[[0-9]+/631\]' "$LOG" | tail -1)
DONE=$(echo "$CURRENT" | grep -oE '[0-9]+' | head -1)
if [ -n "$DONE" ]; then
  PCT=$((DONE * 100 / 631))
  REMAIN=$((631 - DONE))
  # 估算剩余时间：每条约 7 秒 + 限速
  ETA_SEC=$((REMAIN * 8))
  ETA_MIN=$((ETA_SEC / 60))
  echo "   进度：$CURRENT  ($PCT%)"
  echo "   剩余约 ${ETA_MIN} 分钟"
fi

echo
echo "=== 最近 5 行日志 ==="
tail -5 "$LOG"

echo
echo "命令："
echo "   实时跟踪：tail -f $LOG"
echo "   终止：    kill $PID"
