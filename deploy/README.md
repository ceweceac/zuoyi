# 部署指南（Python 版）

## 一键启动

```bash
cd deploy
cp .env.example .env
vi .env                      # 填 DINGTALK_*、LLM_* 凭证
docker compose up -d --build
```

访问：
- 管理后台：http://服务器IP:8088 （admin / admin123）
- 钉钉机器人：直接私聊已配置的机器人

## 目录约定（容器内）

| 路径 | 说明 |
|---|---|
| `/app/app/` | FastAPI 代码 |
| `/app/data/` | SQLite 数据库（持久卷 `qabot-data`） |
| `/app/logs/` | 审计日志（持久卷 `qabot-logs`） |

## 端口

| 端口 | 用途 |
|---|---|
| 8088 | 前端 Nginx（对外）|
| 8080 | 后端 FastAPI（仅 docker 网络内可见）|

## 升级

```bash
git pull
docker compose build
docker compose up -d
```
数据在 docker volume 中，升级不会丢。

## 备份

```bash
docker run --rm -v qa-bot_qabot-data:/data -v $PWD:/backup alpine \
  tar czf /backup/qabot-data-$(date +%F).tar.gz -C /data .
```

## 切换达梦/金仓/MySQL

`.env` 改 `DATABASE_URL`，并在 `backend/requirements.txt` 加对应驱动：
```env
DATABASE_URL=dm+dmPython://user:pwd@host:5236/SCHEMA
```
```
# requirements.txt 追加
dmPython==2.5.5     # 或 psycopg2-binary / pymysql
```
重新 `docker compose build`。SQLAlchemy 自动建表，DDL 通用。

## 信创环境（麒麟 + 鲲鹏）

- Python 基础镜像换成 `openeuler/openeuler:22.03` + `dnf install python3.11`
- 构建 multi-arch：
  ```bash
  docker buildx build --platform linux/amd64,linux/arm64 \
    -f deploy/Dockerfile.backend -t registry.local/qa-bot/backend:0.1.0 --push .
  ```
- Nginx 国密版用 `tengine` + GmSSL

## 安全提醒

- `deploy/.env` 含明文密钥，**绝不能提交到 git**（已在 .gitignore）
- 生产务必更换：钉钉 AppSecret、JWT secret、种子账号密码
- 前端建议挂在 WAF/反代后，启用 HTTPS（国密 SM2 证书）
