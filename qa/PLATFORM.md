# QA 内容测试平台

这是给异地伙伴使用的独立 Web 工作台。测试平台与正式数据库完全隔离，也不会连接钉钉机器人或运行群发任务。默认部署会开放完整管理后台用于代码联调，但伙伴仍受其账号角色权限限制；把 `QA_WORKSPACE_FULL_UI` 改为 `false` 可以恢复只开放 QA 页面的模式。

## 协作流程

1. 伙伴使用 `qa_partner` 登录，只能新增和编辑 QA；每次保存自动进入“待审核”。
2. 负责人使用 `qa_owner` 登录，在“只看待审核”中检查并通过。
3. 负责人点击“下载正式环境变更包”，得到只包含新增、更新、停用项目的 Excel。
4. 对正式库先执行 dry-run：

   ```bash
   python scripts/qa_content_workflow.py import-db \
     --db backend/data/qabot.db \
     --xlsx ~/Downloads/QA正式环境变更包.xlsx
   ```

5. 核对变更数量后才正式写入：

   ```bash
   python scripts/qa_content_workflow.py import-db \
     --db backend/data/qabot.db \
     --xlsx ~/Downloads/QA正式环境变更包.xlsx \
     --apply --approval-mode pending --actor qa-workspace-merge
   ```

正式写入前会自动备份 SQLite；如果正式库的同一条 QA 已被别人修改，版本检查会拒绝覆盖。

## Docker 启动

```bash
cp deploy/qa-workspace.env.example deploy/qa-workspace.env
# 编辑 qa-workspace.env，设置强密码和随机密钥
docker compose --env-file deploy/qa-workspace.env \
  -f deploy/docker-compose.qa-workspace.yml up -d --build
```

浏览器访问 `http://服务器地址:8081`。如需临时公网访问，可以在服务器上运行：

```bash
cloudflared tunnel --url http://127.0.0.1:8081
```

临时隧道地址每次重启会变化；长期使用应配置固定域名和 HTTPS。

不使用 Docker 时，可以准备好 `backend/.qa-workspace.local.env` 后运行：

```bash
python scripts/run_qa_workspace.py
```

## 数据说明

- 初次启动从 `qa/qa_content.xlsx` 导入当前正式 QA 基线。
- 测试数据持久化在独立的 `qa-workspace-data` Docker volume。
- 伙伴账号不能停用或删除 QA；这些操作只有负责人可执行。
- 未审核修改不会进入正式环境变更包。

## 从正式环境刷新完整测试快照

完整联调环境可以继承正式库的 QA、对话、设置、群列表、群发记录和附件。刷新脚本会自动备份旧测试库，并清除正式钉钉凭证、正式群 Webhook、外部 API 密钥，同时禁用所有正式群和定时任务：

```bash
python scripts/refresh_staging_from_production.py \
  --source-db /path/to/production/backend/data/qabot.db \
  --target-db backend/data/qabot-workspace.db \
  --baseline-xlsx backend/data/qa-workspace-baseline.xlsx \
  --source-uploads /path/to/production/backend/data/uploads \
  --target-uploads backend/data/uploads
```

执行前先停止测试服务，完成后再启动。正式库始终只读，不会被脚本修改。

伙伴需要测试群发时，在完整测试后台进入“群发推送 → 群管理 → 新建测试群 Webhook”，填写专用测试群的 Webhook 和 SEC 加签密钥。不要在测试环境填写正式群 Webhook。
