# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

钉钉（DingTalk）企业内部 QA 客服机器人，纯 Python 单体。后端 API、对话流水线、管理后台 UI 全在同一个进程里：

- **FastAPI** — REST API（`app/routers/`，保留给脚本/测试）+ 应用生命周期
- **NiceGUI** — 管理后台 UI，纯 Python 写页面，挂在 FastAPI 根路径（`app/ui/`）
- **dingtalk-stream** — 钉钉 Stream 长连接（出向连接，无需公网入口），机器人即问即答
- **SQLAlchemy** — SQLite（默认）/ 达梦 / 人大金仓 / MySQL，靠 `DATABASE_URL` 切换，自动建表
- 所有源码与注释为中文，**保持中文**。

## Commands

所有命令在 `backend/` 目录下、激活 venv 后运行。注意机器是 Python 3.9，venv 内是 3.10+，习惯用 `.venv/bin/python` 直接调以避免 shebang 问题。

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 填 JWT_SECRET / LLM_API_KEY / 钉钉凭证

# 本地启动（NiceGUI 要求单进程，不要加 --workers）
uvicorn app.main:app --host 0.0.0.0 --port 8080
# 浏览器 http://localhost:8080 ，登录 admin / admin123
```

启动会失败若 `JWT_SECRET` 缺失或弱（<32 字节）。开发期临时绕过：`export QABOT_ALLOW_WEAK_JWT=1`。
源码含 U+FFFD（`�`）损坏字符会**拒绝启动**；跳过：`export QABOT_SKIP_CHARCHECK=1`。

部署（Docker，对外端口 8088 → 容器 8080）：
```bash
cd deploy && cp .env.example .env && docker compose up -d --build
```

### 测试 / 回归（无 pytest，用 `scripts/` 里的独立脚本）

脚本通过 `sys.path.insert` 引入 `backend/`，必须在 `backend/` 目录下用 venv 跑：
```bash
cd backend
.venv/bin/python ../scripts/regression_test.py      # 用真实对话回归 matcher 命中率（改 tags 前后对比）
.venv/bin/python ../scripts/test_at_all.py          # 群发 @全体 测试
EMB_BACKEND=ollama .venv/bin/python ../scripts/shadow_compare.py --n 50   # 字面 vs 语义检索对比（实验性）
```

## Pipeline architecture（核心）

入口 `services/pipeline.py::handle(sender, raw_text, sender_name)` 是整条对话编排，**顺序敏感**——每一层是一道防线，命中即提前返回。理解这个顺序是改动这个仓库的前提：

1. **非文本消息** (`[NON_TEXT:...]`，由 `bot.py` 打标) → 直接转人工
2. **空意图保护** → 友好提示，不调 LLM（防止 LLM 拿历史上下文乱发挥）
3. **闲聊/打招呼/身份提问保护** → 用 `bot_persona` 人设话术硬编码回，不调 LLM
4. **红线词** (`filters.hit_redline`) → 立即转人工 + urgent 告警
5. **投诉关键词** → 立即转人工 + urgent
6. **充值引导** (`recharge_rules`) → 直接发钉钉文档链接，不走 LLM（链接 100% 原样）
7. **业务 ID 模式** (如 `vid-xxxx`，`escalate_id_prefixes`) → 转人工 + 可选填写链接
8. **用户挫败感/纠错词** → 短反馈兜底或转人工（再次纠错升级 urgent）
9. **连续追问 N 次 / burst 未解决** → 转人工
10. **知识库匹配**（两阶段，见下）→ 命中则 A 级回答（可选 `rephrase` 人设润色）
11. **KB 未命中** → 脱敏 (`filters.mask`) → LLM 兜底 → 敏感词过滤 → 占位符回填 → 加水印 → B 级

回答分级写进 `Conversation.answer_level`：**A** = 命中 KB 标准答案，**B** = LLM 兜底/保护话术，**C** = 转人工。`_count_recent_unsolved_messages` 等 burst 判断依赖这个分级，改分级语义会连带影响转人工触发。

### 两阶段知识库匹配

`matcher.py` 做字面粗筛，`llm.judge_match` 做语义精选：

- **dice 粗筛** (`top_k_candidates`)：中英文 token + 2-gram，`_score` 取 `max(dice, qa端召回率)`。有「实质命中校验」（交集全是通用词如「怎么/处理」→ 0 分）和「图/视频互斥语义降权」(`_mutex_penalty`)。
- **强匹配** (dice ≥ `judge_strong_threshold`，默认 0.92) → 跳过裁判直接命中，省 LLM 调用。
- **弱匹配** → `llm.judge_match` 让 LLM 在 top-K 候选里挑或返回 NONE（裁判 prompt 极严格，「宁可 NONE 不要勉强」）。
- **低分裁判防线**：即使裁判选中，原始 dice <0.30 视为不可信，产品域问题转人工。
- 关掉裁判 (`judge_enabled=false`) 回退纯 dice `best_match`。

`matcher.py` 有进程内缓存 (`_get_kb_cache`)，按 `qa_store.store.version` 失效重建——改 KB 后必须 `store.reload()` 才生效。

## Config & runtime settings（两层）

- `config.py::Settings` (pydantic-settings)：从 `.env` 读默认值，是 `settings` 单例。**注意 `settings` 是可变单例，运行时会被 DB 覆盖。**
- `runtime_settings.py`：`sys_setting` 表存 UI 可改的配置（`EDITABLE_KEYS`），启动 `load_from_db()` 覆盖到 `settings`。敏感字段 (`SECRET_KEYS`: `llm_api_key` / `dingtalk_client_secret` / `alert_secret`) 在 DB 里用 **Fernet 加密** (`crypto.py`)，主密钥来自 `QABOT_MASTER_KEY` 或落地 `data/.master.key`。
- **热更新**：`llm._maybe_reload_settings_changed()` 每 5 秒节流从 DB 重读；`pipeline.handle` 每次开头调它，若影响 system prompt 的字段变了就 `store.reload()`。所以管理员在 UI 改配置**无需重启**即可生效。

`qa_store.py::store.prompt` 是拼好的 LLM system prompt（人设 + 业务背景 + 联系人路由 + 全量 KB + 格式约束）。

## DingTalk 接入要点 (`bot.py`)

- **私聊** (`conversation_type=='1'`) → 走 pipeline 回复用户。
- **群聊** (`=='2'`) → 自动入库该群（`DingtalkGroup`，供群发选群用），**默认不回复**，仅被 `@` 时走 pipeline。群里 @ 的 sender 改写成 `g:{conv_id}:{user_id}`，与私聊上下文隔离。
- **防抖** (`debouncer.py`)：用户连发多条消息，等 1.5s 窗口收齐合并成一次处理（最多 8 条）。处理是同步函数，`asyncio.to_thread` 丢线程池避免阻塞事件循环。先 ACK 钉钉、回复走异步队列。
- 非文本消息（图片/语音/视频/富文本无文字）→ 打 `[NON_TEXT:type]` 标记交给 pipeline 转人工。

## 群发推送 (broadcast)

`broadcaster.py` + `scheduler.py`（APScheduler，时区固定 Asia/Shanghai）+ 4 张表 (`Broadcast`/`BroadcastSchedule`/`DingtalkGroup`/`UploadedFile`)。走**每个群各自的自定义机器人 webhook（`oapi.dingtalk.com/robot/send`）+ HMAC-SHA256 加签**（`_webhook_sign`：`timestamp\nsecret` 签名拼到 URL），**不是** accessToken 那套——加签密钥 `DingtalkGroup.webhook_secret` 在 DB 里走 Fernet 加密（`crypto.encrypt/decrypt`，无前缀老明文兼容）。一条 markdown 气泡内含 @全体 + 文字 + 图片。图片可走钉钉 mediaId（零公网依赖，但 3 天过期需重传）；视频/外链需要 `public_base_url` 公网可达。webhook 地址过 `netguard` SSRF 防线。
- **幂等/重试**：`Broadcast.last_result` 存全量 `success_ids`（成功群 id），重试模式 `skip_already_sent` 据此跳过已成功的群——**别依赖被截断的展示字段 `success[:20]` 做幂等**，否则 >20 群重试会重复 @全体。
- `next_run_at` / `last_run_at` 统一存 **naive UTC**（与 `datetime.utcnow()` 同基准），别转成上海时间再去 tzinfo，否则差 8 小时。

**金山 mgg 模型隔离**：mgg-1~9 只属于金山 provider，**禁止 fanout 到其他代理商**（见用户 memory `feedback_kingsoft_mgg_scope.md`）。涉及群发/provider 路由时遵守此约束。

## 私信推送 (direct push)

**与群发是两套不同机制，别混**。私信（单聊主动通知）走 `accessToken + robotCode` 调新版 OpenAPI `robot/oToMessages/batchSend`（token 放 header `x-acs-dingtalk-access-token`，userIds ≤20 个/批），**不是**群发那套自定义机器人 webhook+加签。
- `robotCode`：配置项 `dingtalk_robot_code`，留空自动回退 `dingtalk_client_id`（内部机器人通常等于 AppKey）。
- 目标用手机号定位：手机号 → userId 走 `topapi/v2/user/getbymobile`；非纯 11 位数字当 staffId 原样用。
- **钉钉后台两项权限依赖**：「机器人发送单聊消息」+「根据手机号查询用户」，缺则 batchSend / getbymobile 报错。
- accessToken 复用 `dingtalk_media._get_access_token()`（缓存+锁）。历史存 `direct_push` 表。
- **未送达 = `invalidStaffIdList`（无效，无意义重试）+ `flowControlledStaffIdList`（限流，进 `failed_userids` 供重试）**，两个列表都要计入，别用 `or` 短路（会把限流列表整个吞掉、误算成功）。
- 代码：`services/direct_pusher.py`，UI 在群发页「私信推送」tab（支持上传 .xlsx/.csv 批量识别手机号）。

## DB & migrations

`db.py`：模型 + `init_db()`（建表 + 轻量迁移 + 索引 + seed）。**没有 Alembic**——新增列靠 `_migrate_*_columns()` 手写 `ALTER TABLE ADD COLUMN`（SQLite 兼容），新增索引靠 `_ensure_indexes()` 的 `CREATE INDEX IF NOT EXISTS`。**给已有表加字段时，必须同时更新对应的 `_migrate_*` 函数**，否则老库不会有新列。

种子账号：`admin/admin123`、`auditor/auditor123`、`editor/editor123`。密码 bcrypt 哈希，明文兼容已下线（非 `$2` 开头一律拒绝）。

## 权限模型

JWT (`security.py`)，4 个角色：`admin`（全部）/ `auditor`（审核 QA）/ `editor`（增改 QA、提待审）/ `viewer`（只读）。REST 用 `require_role(...)` 依赖；NiceGUI 页面用 `app/ui/auth_state.py` 的 `A.role()` / `A.can_edit_qa()` 控制。
- **`current_user` 每次回查 DB**：以 DB 里的 `enabled`/`role` 为准（token 里的 role 仅签发时快照），所以禁用/降权/删除用户后旧 token **立即失效**，不是等 TTL 过期。
- **NiceGUI 回调即服务端逻辑，没有 REST 兜底**：任何写 KB 的 UI 入口（新增/编辑/导入/去重/批量删除）回调体内**必须**自己 `if not A.can_edit_qa(): return`，且按钮也用 `if A.can_edit_qa():` 包起来——只靠隐藏按钮挡不住直接触发的回调。
- **登录有失败限频**（`routers/auth.py`，进程内按 用户名+IP 指数退避锁定）：单进程方案，多副本部署需换共享存储（Redis）。用户不存在也跑一次 dummy bcrypt，消除用户枚举时序差。

## 约定与坑

- **保护层不调 LLM**：闲聊、空消息、纠错短反馈、充值、业务 ID 等都用硬编码/规则回复。改动 pipeline 时不要把这些误接进 LLM——LLM 看到无意图输入会用历史上下文编造。
- **禁止 markdown 输出**：钉钉聊天场景，LLM prompt 在 system 末尾和 user message 双重约束「禁用 markdown / 不超过 150 字 / 不分点」。
- **U+FFFD 防线**：源码、DB 写入、UI 保存都拒绝 `�`。`charcheck.py` 的 `_ALLOW_FILES` 列了合法包含该字符的检测逻辑文件。
- **secrets 绝不入库**：`.env`、`*.key`、`data/`、`*.db` 都在 `.gitignore`。生产务必换 `JWT_SECRET`、钉钉 AppSecret、种子密码、`QABOT_MASTER_KEY`。
- **PII 脱敏在 `pipeline.handle` 开头统一做一次**（`filters.mask`）：凡出网给第三方 LLM 的文本（域路由 / 裁判 / 润色 / 兜底 / 历史）一律用脱敏版；转人工告警内容也脱敏。本地 dice 粗筛（`matcher.*`，不出网）仍用原文以保匹配质量。改 pipeline 时别把原文直接喂给任何 `llm.*` 调用。
- **Fernet 主密钥用绝对路径**（`crypto._KEY_FILE` 锚到 backend 目录，与 db.py 一致）：相对路径会随 cwd 漂移，换 systemd/容器工作目录会找不到旧 key → 静默生成新 key → 历史密文永久不可解。生产优先用 `QABOT_MASTER_KEY` 环境变量。
- **敏感配置的"脱敏占位回写"坑**：UI 给 SECRET 字段（`llm_api_key` 等）展示 `sk-1****wxyz`，`runtime_settings.save_to_db` 检测到提交值是脱敏占位（`_looks_masked`）就跳过写入，保留原密文——否则星号会被加密入库当 key 用。
- **`settings` 是可变单例、会被并发改写**：LLM 连接组字段（base_url/api_key/model/...）读写都过 `runtime_settings.settings_lock`，调用侧用 `llm_conn_snapshot()` 取一致性快照，避免热重载中途新旧字段混搭。
- **任意文件读取防线**：报告下载等"路径来自前端"的入口必须校验 `resolve()` 落在白名单目录内且后缀合法（见 `pages.py` 的 `_safe_report_path`）。
- `lab/semantic_search.py` 是实验性语义检索，**未接入主流水线**，仅 `shadow_compare.py` / `build_kb_index.py` 用。
