---
name: deploy-qabot
description: 安全地重启/上线 qa-bot 生产实例。当被要求"重启机器人""上线""部署""让改动生效""应用配置"时使用。固化"备份→停旧→起新→验健康→验钉钉"的标准流程，避免每次手工重启遗漏步骤导致钉钉断连或数据丢失。
---

# deploy-qabot：生产重启/上线 SOP

固化生产 qa-bot 的安全重启流程。这几天重启 10+ 次都是同一套手工动作，
沉淀成 SOP，避免漏步骤（漏备份丢数据、漏验钉钉导致机器人哑了没人知道）。

## 生产实例事实

- 跑在 **8080 端口**，裸 uvicorn（`app.main:app`），cwd = `backend/`
- 启动命令：`.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080`
- 钉钉走 Stream 出向长连接，凭证在 `backend/.env`
- 数据库：`backend/data/qabot.db`（SQLite）

## 标准重启流程（5 步，缺一不可）

在 `backend/` 目录下执行：

```bash
# 1. 备份生产库（回滚保险，必做）
cp data/qabot.db /tmp/qabot_backup_$(date +%Y%m%d_%H%M%S).db

# 2. 停旧进程（先优雅 kill，3秒后强杀兜底）
OLD=$(lsof -nP -iTCP:8080 -sTCP:LISTEN -t | head -1)
kill "$OLD" 2>/dev/null; sleep 3; kill -9 "$OLD" 2>/dev/null

# 3. 起新进程（生产 .env，连真钉钉）
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 > /tmp/qabot_prod_restart.log 2>&1 &

# 4. 等健康检查（最多 30s）
for i in $(seq 1 60); do
  c=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/api/health 2>/dev/null)
  [ "$c" = "200" ] && echo "✓ 健康 health=200" && break
  sleep 0.5
done

# 5. 验钉钉连接（关键！0 失败才算真上线）
sleep 3
echo "钉钉失败次数: $(grep -c 'open connection failed' /tmp/qabot_prod_restart.log)"
grep -E 'DingTalk Stream|App started' /tmp/qabot_prod_restart.log | tail -2
```

## 验收标准（全过才算成功）

- ✅ `/api/health` 返回 200
- ✅ 钉钉 `open connection failed` 次数 = **0**（否则机器人收不到消息）
- ✅ 日志有 `DingTalk Stream client starting` + `App started`

## 铁律

- **必须先备份库**：重启本身不动库，但若新代码有 DB 迁移，备份是唯一回滚保险。
- **必须验钉钉连接**：health=200 只说明 Web 起来了，钉钉没连上机器人照样是哑的。
- **改了代码/配置才需重启**：纯数据改动（KB 审核通过）会自动 store.reload()，不用重启；
  改了 .py 代码、.env、或脚本直接改了 approved 条目，才需重启。
- **绝不在重启高峰期**：钉钉用户消息会在重启的几秒中断窗口里漏处理。
- **改动先过 verifier-qabot**：上线前先在隔离实例验证（见 verifier-qabot skill），别拿生产试。

## 配套：QABOT_DISABLE_BOT

测试/CI 环境无钉钉凭证时，加 `QABOT_DISABLE_BOT=1` 跳过钉钉连接，
让 Web/UI 干净启动（生产绝不设此变量）。verifier-qabot 的隔离实例已内置。
