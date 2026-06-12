---
name: verifier-qabot
description: 验证 qa-bot 代码改动的"运行时行为"——安全地起一个隔离实例（不碰生产、不连真钉钉、不发任何消息），驱动真实接口观察行为。当被要求 verify qa-bot 的 PR/改动、确认某修复在跑起来的系统里真的生效、push 前做行为把关时使用。
---

# verifier-qabot：qa-bot 的行为验证协议

这是 qa-bot 的"证据捕获配方"。任何要 **verify**（运行时观察，而非读代码/跑单测）
qa-bot 改动的会话，都先读它、按它来，保证：① 绝不碰生产；② 复现可回放。

## 铁律：绝不碰生产

- 生产实例跑在 **8080（容器内）/ 8088（对外）**。**任何验证都不在其上操作**——
  会真改生产 KB、真发钉钉群发/告警/私信（这些都是 verify 的 destructive-path，
  无安全靶子时禁止 live 驱动）。
- 所有验证只在**隔离实例**（端口 18080，临时库，禁钉钉）上做。

## 第 0 步：起隔离实例

```bash
scripts/verify_launch.sh up      # 起 18080 隔离实例，等 /api/health=200
# 看到 "VERIFY_READY http://127.0.0.1:18080" 即就绪
# 用完务必：
scripts/verify_launch.sh down    # 停实例 + 删临时库
```

隔离保证（脚本已内置，原理见脚本注释）：
- `QABOT_DISABLE_BOT=1` → **完全不启动钉钉 Stream**：不连真钉钉、不发任何消息，
  也避免钉钉连接重试阻塞 ASGI 启动（这是实跑才发现的坑：无此开关时 /api/health 永不就绪）。
- 临时 SQLite `/tmp/qabot_verify.db`，种子自动建账号；不碰生产库、不碰 `backend/.env`。
- `LLM_ENABLED=false` 不调真 LLM；临时 JWT secret + 弱密钥绕过。

种子账号（验授权类改动用）：
`admin/admin123`（全权） · `editor/editor123`（可改 KB） · `auditor/auditor123`（仅审核） · viewer（只读，需自建）

## 第 1 步：按 surface 选驱动方式

| 改动落在 | 真 surface | 怎么驱动 |
|---|---|---|
| REST 路由 (`app/routers/`) | HTTP socket | `curl` 打 `/api/...`，看状态码/响应体 |
| 授权 (`security.py` / `auth_state.py`) | HTTP + 角色 | 各角色登录拿 token，调受保护端点看 403/200 |
| 对话流水线 (`pipeline.py`) | `pipeline.handle()` | 隔离实例进程内直接喂 `handle(sender, text)`，看分级/转人工 |
| NiceGUI 页面 (`app/ui/`) | 浏览器（WebSocket 渲染）| 见下方说明——本机无 Playwright 时退到判据层并在报告中注明 |

### 登录拿 token（HTTP 授权验证模板）
```bash
B=http://127.0.0.1:18080
TOKEN=$(curl -s -X POST $B/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"auditor","password":"auditor123"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")
curl -s -o /dev/null -w "HTTP=%{http_code}\n" -X POST $B/api/qa \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"q","answer":"a"}'   # auditor/viewer 应 403，editor/admin 应 200
```

### NiceGUI UI 层的诚实说明
NiceGUI 页面是 WebSocket 服务端渲染，headless curl 抓不到按钮状态。本机无 Playwright 时，
UI 层授权改动退到**判据层**验证（直接驱动 `auth_state.can_edit_qa()` 等对各角色的裁决），
并在报告里写明 surface 妥协（"未用浏览器点按钮，验的是按钮背后的授权判据"）。
要做到真·浏览器 surface，需先 `pip install playwright && playwright install chromium`。

## 第 2 步：push on it（探边界，至少一个 🔍）

确认 happy path 只是上半场。在同一 surface 探：
- 授权改动 → 无 token（应 401）、过期 token、错角色、空 body
- 路由改动 → 错方法、缺必填字段、超大 body
- pipeline 改动 → 空消息、连发、红线词/投诉词边界

## 第 3 步：按 verify 格式报告

```
## Verification: <一句话改了什么>
**Verdict:** PASS | FAIL | BLOCKED | SKIP
**Claim:** <对 diff 的理解；与描述有出入也写>
**Method:** verifier-qabot 隔离实例 (18080) + <HTTP/判据层/...>
### Steps
1. ✅/❌/⚠️/🔍 <对运行实例做了什么> → <观察到什么>（贴真实输出）
### Findings
<跑起来才发现的东西，不止 bug——摩擦、意外、surface 妥协都写>
```

裁决铁律：无"部分通过"（3/4 即 FAIL）；**存疑即 FAIL**；纯文档/类型/测试改动且无运行时 surface → SKIP。

## 不该用本配方的情况（诚实边界）

- **改动正确性靠读代码就能高置信确认**（如变量重命名、常量调整）→ 跑实例是仪式，直接 code review。
- **改动是 git 历史/仓库状态层面**（如清除入库密钥）→ 无运行时 surface，SKIP。
- **必须真发钉钉才能观察的功能**（群发/私信/转人工告警的实际投递）→ 隔离实例发不了，
  只能验到"调用前"，投递本身需独立的测试钉钉应用，报告里注明未覆盖。
