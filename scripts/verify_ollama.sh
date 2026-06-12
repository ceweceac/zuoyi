#!/usr/bin/env bash
# 验证 Ollama 是否能被 qa-bot 的 llm.py 直接调用
# 用法：bash scripts/verify_ollama.sh
set -e

BASE_URL="${LLM_BASE_URL:-http://localhost:11434/v1}"
API_KEY="${LLM_API_KEY:-ollama}"
MODEL="${LLM_MODEL:-qwen2.5:14b}"

echo "==> 1) 检查 Ollama 进程是否在跑"
if ! curl -s -m 3 "${BASE_URL%/v1}/api/tags" >/dev/null; then
  echo "❌ Ollama 服务没起来。先跑：brew services start ollama"
  exit 1
fi
echo "✅ Ollama 服务在 ${BASE_URL%/v1}"

echo
echo "==> 2) 检查 OpenAI 兼容 /v1/models"
MODELS=$(curl -s -m 5 "${BASE_URL}/models" -H "Authorization: Bearer ${API_KEY}")
echo "$MODELS" | head -c 300; echo
if ! echo "$MODELS" | grep -q "${MODEL%:*}"; then
  echo "⚠️  模型 ${MODEL} 未拉。先跑：ollama pull ${MODEL}"
  exit 1
fi
echo "✅ ${MODEL} 已就绪"

echo
echo "==> 3) 模拟 qa-bot 的 llm.ask() 真实请求体"
RESP=$(curl -s -m 60 "${BASE_URL}/chat/completions" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"${MODEL}\",
    \"temperature\": 0.7,
    \"max_tokens\": 100,
    \"messages\": [
      {\"role\": \"system\", \"content\": \"你叫小灵，部门里的同事。\"},
      {\"role\": \"user\", \"content\": \"公司几点上班？\"}
    ]
  }")
echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print('✅ 回答:', d['choices'][0]['message']['content'][:80]); u=d.get('usage',{}); print(f'   tokens: prompt={u.get(\"prompt_tokens\")} completion={u.get(\"completion_tokens\")}')"

echo
echo "==> 4) 模拟 judge_match() 严格 JSON 输出"
JUDGE=$(curl -s -m 60 "${BASE_URL}/chat/completions" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"${MODEL}\",
    \"temperature\": 0.0,
    \"max_tokens\": 50,
    \"messages\": [
      {\"role\": \"system\", \"content\": \"严格只输出 JSON {\\\"choice\\\":<编号>,\\\"reason\\\":\\\"<15字>\\\"}\"},
      {\"role\": \"user\", \"content\": \"用户问'年假怎么请'，候选[1]'如何申请年假'\"}
    ]
  }")
JSON_OUT=$(echo "$JUDGE" | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'][:120])")
echo "裁判输出: $JSON_OUT"
if echo "$JSON_OUT" | grep -qE '\{.*"choice".*\}'; then
  echo "✅ judge_match() 路径通过（JSON 格式可解析）"
else
  echo "⚠️  模型 JSON 输出不稳，建议换更大模型（qwen2.5:32b / deepseek-r1:14b）"
fi

echo
echo "==> 全部验证通过。把 backend/.env 里 LLM_ENABLED 改成 true 然后重启服务即可"
