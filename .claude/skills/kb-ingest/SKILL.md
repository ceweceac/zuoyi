---
name: kb-ingest
description: 把产品文档/手册转成 qa-bot 客服机器人的知识。当被要求"把某文档导入知识库""让机器人学习某产品/功能""根据文档补充 QA""更新机器人对XX的认知"时使用。覆盖从文档拆 QA、去重、打标、导入待审到失败回流的完整知识库运营流水线。
---

# kb-ingest：文档 → 机器人知识 流水线

把散落在 scripts/ 的 8 个知识库运营脚本串成一条有序流程。
以后导任何产品文档都走这条线，不用每次重记脚本顺序和参数。

## 核心认知：机器人有「两条知识线」，先判断走哪条

| 线 | 存哪 | 适合什么内容 | 用什么脚本 |
|---|---|---|---|
| **QA 知识库**（精确问答） | `qa_item` 表 | 有明确问法的点状知识："怎么建画布""图生视频怎么用" | `import_dramatv_qa.py` |
| **product_background**（产品手册） | `sys_setting` 表 | 需要 LLM"读过全文"才能推理的背景：产品定位、机制原理 | `inject_dramatv_background.py` / `learn_dramatv_doc.py` |

判断规则：**能拆成「一问一答」的 → QA 线；讲不清边界、需要整体理解的背景 → background 线。**
（详见 docs/机器人认知双线.md）

## 标准流程（按序）

所有命令在 `backend/` 目录、激活 venv 后跑。

### 第 1 步：差异分析（可选，避免重复造）
先看文档里哪些内容 KB 已有、哪些是缺口：
```bash
.venv/bin/python ../scripts/dramatv_gap_analysis.py   # 文档按##/###切块 vs 现有KB比对
```

### 第 2 步：拆 QA → Excel
把文档可问答内容拆成 QA，落成 Excel（列序固定）：
- A=来源章节 B=问题 C=答案 D=分类 E=标签 F=审核状态(空=入库/SKIP=跳过)
- **分类必须用 6 个标准类之一**：功能介绍 / 操作流程 / 使用技巧 / 问题处理 / 账号与权限 / 合规规范
- 缺口型 QA 可用 `gen_dramatv_gap_qa.py` 半自动生成

### 第 3 步：导入（先 dry-run）
```bash
.venv/bin/python ../scripts/import_dramatv_qa.py --file data/<你的>.xlsx --dry-run  # 预览+看去重
.venv/bin/python ../scripts/import_dramatv_qa.py --file data/<你的>.xlsx           # 真导入
```
- 入库 `status=pending`，question 完全相同自动去重
- **导入后必须查语义重复**：问法不同但答同一事的，删新留旧（旧答案常更详细）

### 第 4 步：打标签（提升命中率）
```bash
.venv/bin/python ../scripts/gen_qa_tags.py        # 给空tags的QA生成多问法变体
.venv/bin/python ../scripts/import_qa_tags.py     # 人工审核后导回
.venv/bin/python ../scripts/label_qa_domains.py --only-empty   # 打业务域标签(域路由用)
```

### 第 5 步：审核生效（只能人工）
浏览器进 `/qa` → 状态筛「待审核」→ 逐条审 → 点【通过】。
通过后 `store.reload()` 自动热更新，机器人即可用新知识。

### 第 6 步：失败回流（持续运营）
定期把"机器人没答好"的真实问题回流补 KB：
```bash
EMB_BACKEND=ollama .venv/bin/python ../scripts/failure_replay.py --limit 50
```

## 关键纪律

- **导入后查重复**：完全相同问题导入会去重，但「问法不同答同一事」要人工查（用 Jaccard≥0.45 比对问题）。
- **分类只用 6 个标准类**：别再造"功能/产品功能/功能操作"这种重叠类（曾经 43 种，已收敛）。
- **pending 不自动生效**：必须人工审核，这是防止机器人乱答的最后一道闸。
- **改 KB 后要 reload**：审核通过会自动 reload；脚本直接改库的，生产实例需重启或调 /api/qa/reload 才生效。
- **去重删新留旧**：原有人工 QA 往往答案更细，新导入的让位。

## 不该用本流水线的情况

- 内容是**模型计费/用量/掺水检测**相关 → 那是另一套系统（API健康检查平台），不进客服机器人 KB。
- 一次性临时问答、与产品无关的闲聊 → 不入库。
