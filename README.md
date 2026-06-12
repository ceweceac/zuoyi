# 钉钉 QA 自动回复机器人（纯 Python 单体）

钉钉企业内部应用 + 大模型驱动的 QA 客服机器人，**后端 + 管理界面全部 Python**。

- 后端 / API：**FastAPI** + SQLAlchemy + dingtalk-stream
- 管理界面：**NiceGUI**（纯 Python 写 UI，与 FastAPI 同进程）
- 数据：SQLite（默认）/ 达梦 / 人大金仓 / MySQL（改 `DATABASE_URL`）

```
qa-bot/
├── backend/
│   ├── app/
│   │   ├── main.py             FastAPI + NiceGUI 入口
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── security.py
│   │   ├── bot.py              钉钉 Stream 客户端
│   │   ├── routers/            REST API（保留供脚本/测试）
│   │   ├── services/           filters / llm / audit / pipeline / qa_store
│   │   └── ui/                 NiceGUI 页面
│   │       ├── auth_state.py
│   │       └── pages.py        登录、概览、QA 管理、对话记录、未命中
│   ├── requirements.txt
│   └── .env.example
└── deploy/                     Docker Compose 单服务一键部署
```

## 本地启动

需要 **Python 3.10+**（你机器 3.9 也能跑，但 venv shebang 需 ./.venv/bin/python 直接调）。

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # 填 LLM_API_KEY 等
uvicorn app.main:app --port 8080
```

浏览器打开 http://localhost:8080 ，登录 `admin / admin123`。

## 一键部署

```bash
cd deploy
cp .env.example .env
docker compose up -d --build
```
访问 http://服务器IP:8088

## 能力一览

- 钉钉 Stream 模式接入，私聊机器人即问即答
- OpenAI 兼容协议 LLM：通义 / DeepSeek / Kimi / 智谱 / 本地 vLLM
- 多层防线：红线词 → 脱敏 → LLM → 敏感词 → 占位符回填 → 分级与水印
- QA 知识库：增删改查、审核流、Excel 批量导入、热更新
- NiceGUI 管理后台：登录、概览 KPI、QA 管理、对话记录、未命中聚合
- 审计：JSON 行文件 + DB 双写
- JWT 鉴权：admin / auditor / editor / viewer

## 配置 LLM

`backend/.env` 或 `deploy/.env`：
```env
LLM_ENABLED=true
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-xxxxx
LLM_MODEL=qwen-plus
```

| 厂商 | base_url | model 示例 |
|---|---|---|
| 阿里通义 | https://dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus / qwen-max |
| DeepSeek | https://api.deepseek.com/v1 | deepseek-chat |
| Moonshot Kimi | https://api.moonshot.cn/v1 | moonshot-v1-8k |
| 智谱 GLM | https://open.bigmodel.cn/api/paas/v4 | glm-4-plus |
| 本地 vLLM | http://localhost:8000/v1 | 你的模型名 |

## 切换企业数据库

```env
# 达梦
DATABASE_URL=dm+dmPython://user:pwd@host:5236/SCHEMA
# 人大金仓
DATABASE_URL=postgresql+psycopg2://user:pwd@host:54321/dbname
# MySQL
DATABASE_URL=mysql+pymysql://user:pwd@host:3306/qabot
```
对应安装驱动（`pip install dmPython` / `psycopg2-binary` / `pymysql`），SQLAlchemy 自动建表。

## 安全提醒

- `.env` 已加入 `.gitignore`，绝不要把含密钥的文件入库
- 生产部署务必更换：钉钉 AppSecret（已外泄请去开放平台重置）、JWT secret、种子账号密码
- 前端建议挂在公司 WAF/Nginx 反代后并启用 HTTPS

更多见 [backend/README.md](backend/README.md) 与 [deploy/README.md](deploy/README.md)。
