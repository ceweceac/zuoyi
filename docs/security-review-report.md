# 安全 & 代码审查报告

> 生成日期：2026-06-12
> 工具：Claude Code `/code-review`、`/security-review` skill + 人工审查
> 范围：qa-bot 全代码库（backend/app ~9700 行 + scripts），以及本地 Claude Code 配置

---

## 0. 背景

qa-bot 是钉钉企业内部 QA 客服机器人（FastAPI + NiceGUI + dingtalk-stream + SQLAlchemy 单体）。
本次对整个代码库做了三轮检查：

1. **人工安全审查 + sub-agent 代码正确性审查**（skill 因仓库无 git diff 基线无法直接启动时的替代方案）
2. **`/code-review`**（high effort，7 finder angle）——审本次修复的 diff
3. **`/security-review`**（3 步子任务法）——构造空树基线后审全代码库

---

## 1. 修复汇总表

| # | 严重度 | 文件 | 问题 | 状态 |
|---|---|---|---|---|
| 1 | HIGH | `backend/.nicegui/*` | 192 个会话文件含登录 JWT 被入库（含 admin 角色） | ✅ 已清除 + 重写历史 |
| 2 | MEDIUM | `ui/pages.py` | QA `save()` 缺角色校验，viewer/auditor 可越权改 KB | ✅ 已修 |
| 3 | HIGH(回归) | `services/alert.py` | SSRF 校验覆盖入参 `reason`，告警卡片"触发原因"全空 | ✅ 已修 |
| 4 | MEDIUM | `services/netguard.py` | SSRF 硬拦内网，误伤内网中继 webhook 部署 | ✅ 加逃生开关 |
| 5 | 正确性 | `services/pipeline.py` | 充值回复被误计"未解决"→ 连问几次被强制转人工 | ✅ 已修 |
| 6 | 正确性 | `services/pipeline.py` | LLM 兜底 token 计费覆盖裁判 token，统计偏低 | ✅ 已修 |
| 7 | 正确性 | `services/pipeline.py` | `repeat_threshold` 下限 1 → 每条消息都转人工 | ✅ 下限改 2 |
| 8 | 正确性 | `routers/qa.py` `ui/pages.py` | 编辑 QA 后未 `store.reload()`，继续用旧答案应答 | ✅ 已修 |
| 9 | 正确性 | `services/debouncer.py` | `pending_after` 丢 msg_obj，第二轮用错 sessionWebhook | ✅ 已修 |
| 10 | 正确性 | `db.py` | broadcast/broadcast_schedule 两表缺迁移函数 | ✅ 已补 |
| 11 | 安全 | `services/crypto.py` `runtime_settings.py` | 解密失败静默返回空串，密钥无声变空难排查 | ✅ 改抛 DecryptError + 告警 |
| 12 | 安全 | `services/{broadcaster,alert}.py` | 管理员填的 webhook 服务端请求无 SSRF 防护 | ✅ 接入 netguard |
| 13 | 安全 | `ui/pages.py` | 对话记录 CSV 导出无公式注入防护 | ✅ 已修 |
| 14 | 加固 | `main.py` | `QABOT_UI_STORAGE_SECRET` 未设时多副本会话不一致 | ✅ 加启动告警 |

---

## 2. 重点漏洞详情

### #1 HIGH — 入库的会话 JWT token

**事实**：`backend/.nicegui/` 下 192 个 NiceGUI 会话存储文件被 git 跟踪并提交，每个含
`app.storage.user["token"]`（`auth_state.py:27` 写入）= 登录 JWT，部分会话角色为 `admin`，
TTL 480 分钟，JWT 密钥全局共享。`.gitignore` 当时未排除 `.nicegui/`。

**影响**：任何能读到仓库（克隆/备份/推远端）的人可重放 token 获得对应角色（含 admin）的后台权限——
用户管理、系统设置（读解密后密钥）、群发，零凭证。

**处置**：
- `git rm -r --cached backend/.nicegui/` + `.gitignore` 增加 `.nicegui/` 等规则
- **`git filter-branch` 重写全部历史**，从所有 commit 与 git 对象抹除（已验证历史引用 0、对象 0）
- reflog 过期 + `git gc --prune=now` 删除悬空对象，不可恢复

**⚠️ 仍需运维做**：轮换 `JWT_SECRET`。若仓库曾 push 远端或被克隆过，旧密钥签名的 token 仍有效，
换密钥才能彻底失效（现存会话需重新登录）。

### #2 MEDIUM — viewer/auditor 越权改 KB

`/qa` 页仅 `_require_login()` 把关；同页 approve/disable/delete 都有角色校验，唯独执行新增/编辑的
`save()` 没有。NiceGUI 回调在服务端执行，故为真实服务端授权缺口。低权限用户可篡改 KB，
或把已审核的线上条目改成 pending 后 `store.reload()` 使其从服务中消失。
**修复**：`save()` 入口加 `A.can_edit_qa()`，新增按钮对无权限角色隐藏。

### #3/#4 SSRF 相关（由 /code-review 在本次修复中发现）

- #3 是引入 SSRF 校验时把返回值赋给了与入参同名的 `reason`，导致所有告警卡片"触发原因"为空——
  改名 `block_reason`。
- #4 SSRF 默认硬拦内网会误伤把 webhook 指向内网中继的合法部署——加环境变量逃生开关
  `QABOT_ALLOW_INTRANET_WEBHOOK=1`（默认仍拦内网；**云元数据 169.254 / link-local 始终拦截**，不受开关影响）。

---

## 3. 审查为"安全/无需处理"的项（记录备查）

- **SQL 注入**：全部走 SQLAlchemy ORM/绑定参数；`_migrate_*` 的 ALTER 只拼硬编码列名；qa.py LIKE 已转义通配符。
- **JWT 算法混淆**：`security.py` 固定 `algorithms=[HS256]`，无 none-alg 路径。
- **XSS via ui.html**：所有 `ui.html(f'...')` 只插服务端常量/数字，用户可控字段走 `ui.table`/`ui.label`（自动转义）。
- **代码执行/反序列化**：无 `eval`/`exec`/`pickle`/`yaml.load`/`os.system`/`subprocess`；ingest 用安全解析器。
- **failure-reports 任意文件读**：技术成立但 admin-only、admin 本就有等价权限，纵深防御问题（confidence 4/10，未达报告阈值）。建议仍加 `basename`/`is_relative_to` 校验。
- **身份提问先于产品域匹配**：有意设计（企业 bot 必须用配置人设名回答身份问题），保留。

---

## 4. 系统配置（本地 Claude Code 环境）

- `~/.claude/settings.json` 权限 644 → 600（明文 `ANTHROPIC_AUTH_TOKEN` 同机可读）
- `settings.local.json` 删除 3 条 `sudo` 永久授权，chmod 600
- **⚠️ 仍需手动**：轮换 `ANTHROPIC_AUTH_TOKEN`（明文存储且已暴露）

---

## 5. 遗留待办（仅运维/人工可做）

1. **轮换 `JWT_SECRET`** — 使已泄露的会话 token 失效
2. **轮换 `ANTHROPIC_AUTH_TOKEN`** — 本地配置里明文暴露过
3. 生产多副本部署时显式设置 `QABOT_UI_STORAGE_SECRET`（同值）
