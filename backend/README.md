# qa-bot 后端（FastAPI + NiceGUI）

后端和管理界面在同一个 Python 进程里。

## 一、依赖
- Python 3.10+（3.9 也能跑，shebang 路径需用 ./.venv/bin/uvicorn）

## 二、本地启动
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8080
```
浏览器打开 http://localhost:8080 ，登录 admin / admin123。

## 三、目录
- app/main.py        入口（FastAPI + NiceGUI）
- app/config.py      Settings
- app/db.py          ORM + 种子
- app/security.py    JWT
- app/bot.py         钉钉 Stream
- app/routers/       REST API（保留）
- app/services/      filters/llm/audit/qa_store/pipeline
- app/ui/            NiceGUI 页面

## 四、生产部署
单进程：`uvicorn app.main:app --host 0.0.0.0 --port 8080`（NiceGUI 推荐 workers=1）
