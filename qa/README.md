# QA 内容协作流程

`qa-staging` 是 QA 内容的测试/审核分支，`main` 是正式分支。

## 伙伴开始修改

```bash
git clone https://github.com/ceweceac/zuoyi.git
cd zuoyi
git switch qa-staging
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

1. 打开 `qa/qa_content.xlsx`。
2. 只修改「QA内容」工作表的黄色列。
3. 修改已有问答：保留 `KEEP`、`ID` 和「版本」，直接修改问题/答案，并填「修改说明」。
4. 新增问答：使用表尾 `ADD` 空白行，`ID` 留空。
5. 停用问答：把「操作」改为 `DISABLE`，不要删除整行。
6. 不要使用 Excel 公式，不要改 `ID`/「版本」。

提交前校验并生成 Git 可读的 CSV：

```bash
python scripts/qa_content_workflow.py sync-csv
git add qa/qa_content.xlsx qa/qa_content.csv
git commit -m "QA: 更新问答内容"
git push origin qa-staging
```

Excel 是导入源，CSV 用于 Pull Request 中查看逐行差异，两者必须一起提交。

## 创建独立测试库

测试库文件不会进 Git：

```bash
python scripts/qa_content_workflow.py init-test-db \
  --db backend/data/qabot-test.db
```

启动独立的本地测试后台：

```bash
cd backend
DATABASE_URL=sqlite:///./data/qabot-test.db \
QABOT_DISABLE_BOT=1 \
QABOT_ALLOW_WEAK_JWT=1 \
../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8081
```

启动后可在 `http://127.0.0.1:8081/qa` 检查问答。测试环境不连钉钉机器人。

已有测试库需同步新修改时，先 dry-run：

```bash
python scripts/qa_content_workflow.py import-db \
  --db backend/data/qabot-test.db \
  --approval-mode approved
```

确认变更数量后才写入：

```bash
python scripts/qa_content_workflow.py import-db \
  --db backend/data/qabot-test.db \
  --approval-mode approved \
  --apply
```

## 合并到正式分支

1. 在 GitHub 创建 Pull Request：`qa-staging` → `main`。
2. 核对 CSV 差异、测试结果和「修改说明」。
3. 审核通过后合并 Pull Request。
4. 合并代表内容已审核，但不会自动改正式数据库；部署时还要执行一次正式库导入。

正式库先 dry-run（默认把新增/修改条目转为 `pending`）：

```bash
python scripts/qa_content_workflow.py import-db \
  --db backend/data/qabot.db
```

确认后写入：

```bash
python scripts/qa_content_workflow.py import-db \
  --db backend/data/qabot.db \
  --apply
```

每次实际写入前都会在原数据库旁自动生成带时间戳的 `.bak.*` 备份。新增/修改的 `pending` 条目需在正式后台再点「通过」才会生效。
