#!/usr/bin/env python3
"""
批量给没填 tags 的 KB 条目生成多问法（变体问句）。

工作方式：
1) 读 backend/data/qabot.db 里 status=approved + tags 空 的 QA
2) 调你已配置的 LLM（doubao-seed-1.6-flash）给每条生成 5 个变体问法
3) 输出 data/tags_review.xlsx，3 列：id / 原问 / 候选 tags（逗号分隔）
4) 你打开 Excel 人工审一遍（删错的、加缺的），然后跑 A2 导回 DB

特点：
- 用项目自己的 db.py + config.py，配置和你后台完全一致
- 不动 .py 业务代码，只读 + 输出 Excel
- 失败/超时单条跳过，可重跑（断点续接：已生成的会跳过）
- 限速：默认每 2 条 sleep 1 秒，避免触发上游 QPS 限制
- DRY RUN 模式：加 --dry-run 只跑 10 条试效果

用法（在 backend 目录里跑，确保用项目 venv 的依赖）：
  cd backend
  .venv/bin/python ../scripts/gen_qa_tags.py            # 全量
  .venv/bin/python ../scripts/gen_qa_tags.py --dry-run  # 只跑 10 条
  .venv/bin/python ../scripts/gen_qa_tags.py --limit 50 # 自定义条数
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from pathlib import Path

# 让脚本能 import 项目模块
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal, QaItem  # noqa: E402
from app.services import runtime_settings  # noqa: E402
from app.config import settings  # noqa: E402

import httpx  # noqa: E402
import openpyxl  # noqa: E402

OUTPUT = BACKEND / "data" / "tags_review.xlsx"

PROMPT_TPL = """你是企业知识库整理助手。下面是一条标准 QA：

【标准问题】{question}
【标准答案】{answer}

请生成 5 个用户可能用来问同一件事的【变体问法】（不同表达、口语化、含简称/缩写/同义词），用于知识库的多问法匹配召回率提升。

要求：
1. 必须保持核心动作和对象不变（"申请年假" → 不能变成"取消年假"）
2. 不要过于发散（不要泛化成"如何休假"这种类别词）
3. 至少 1 条用口语（"咋"、"啥"、"咋办"），至少 1 条含同义词
4. 每条 5-25 字，不要太长
5. 输出 JSON 数组，无前缀后缀、无 markdown：
   ["变体1", "变体2", "变体3", "变体4", "变体5"]
"""


def call_llm(question: str, answer: str, timeout: int = 30) -> list[str] | None:
    """调 LLM 生成变体。失败返回 None。"""
    if not settings.llm_enabled or not settings.llm_api_key or not settings.llm_base_url:
        raise RuntimeError("LLM 未启用，先在 /settings 里配好")

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "temperature": 0.7,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": "你只输出 JSON 数组，不要任何额外文本。"},
            {"role": "user", "content": PROMPT_TPL.format(question=question, answer=answer[:300])},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code != 200:
            print(f"  ⚠️ HTTP {r.status_code}: {r.text[:120]}")
            return None
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        # 容错：剥 markdown 围栏
        m = re.search(r"\[[\s\S]*?\]", content)
        if not m:
            print(f"  ⚠️ 无 JSON: {content[:80]}")
            return None
        arr = json.loads(m.group(0))
        if not isinstance(arr, list):
            return None
        # 去重 + 去和原问相同的
        out = []
        seen = {question.strip()}
        for v in arr:
            v = str(v).strip().strip("「」\"'""''")
            if v and v not in seen and len(v) <= 40:
                out.append(v)
                seen.add(v)
        return out[:6]
    except Exception as e:
        print(f"  ⚠️ 异常: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只跑 10 条")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--resume", action="store_true",
                        help="续跑：如果 Excel 已存在，跳过已生成的 id")
    args = parser.parse_args()

    # 加载 DB 覆盖到 settings（LLM 配置等）
    runtime_settings.load_from_db()
    print(f"使用 LLM: {settings.llm_model} @ {settings.llm_base_url}")
    if not settings.llm_enabled:
        print("❌ LLM 未启用。先去 /settings 打开 LLM。")
        sys.exit(1)

    db = SessionLocal()
    try:
        rows = db.query(QaItem).filter(
            QaItem.status == "approved",
            QaItem.deleted == "0",
            (QaItem.tags.is_(None)) | (QaItem.tags == ""),
        ).order_by(QaItem.id).all()
    finally:
        db.close()

    total = len(rows)
    if args.dry_run:
        rows = rows[:10]
    elif args.limit > 0:
        rows = rows[:args.limit]

    print(f"待处理 {len(rows)} 条（数据库共 {total} 条无 tags）")

    # 续跑：读 Excel 里已有的 id
    done_ids = set()
    if args.resume and OUTPUT.exists():
        wb = openpyxl.load_workbook(OUTPUT)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row and row[0]:
                done_ids.add(int(row[0]))
        print(f"续跑模式：已有 {len(done_ids)} 条，跳过这些")
        wb.close()

    # 准备 Excel
    if OUTPUT.exists() and args.resume:
        wb = openpyxl.load_workbook(OUTPUT)
        ws = wb.active
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "tags_review"
        ws.append(["id", "原问题", "原答案(前60字)", "建议tags(逗号分隔，可改)"])
        # 列宽
        ws.column_dimensions['A'].width = 8
        ws.column_dimensions['B'].width = 50
        ws.column_dimensions['C'].width = 50
        ws.column_dimensions['D'].width = 80

    ok = fail = skip = 0
    t0 = time.time()
    for i, it in enumerate(rows, 1):
        if it.id in done_ids:
            skip += 1
            continue
        print(f"[{i}/{len(rows)}] id={it.id} q={it.question[:30]}", flush=True)
        variants = call_llm(it.question, it.answer or "")
        if variants:
            ws.append([it.id, it.question, (it.answer or "")[:60], ",".join(variants)])
            ok += 1
        else:
            ws.append([it.id, it.question, (it.answer or "")[:60], ""])
            fail += 1
        # 限速：每 2 条 sleep 1 秒
        if i % 2 == 0:
            time.sleep(1)
        # 每 20 条存一次盘（防中途崩了丢进度）
        if i % 20 == 0:
            wb.save(OUTPUT)
            print(f"  💾 中途保存（{i}/{len(rows)}）")

    wb.save(OUTPUT)
    elapsed = time.time() - t0
    print(f"\n完成：成功 {ok} / 失败 {fail} / 跳过 {skip} / 耗时 {elapsed:.0f}s")
    print(f"输出文件：{OUTPUT}")
    print(f"\n下一步：用 Excel 打开审核 → 跑 scripts/import_qa_tags.py 导回 DB")


if __name__ == "__main__":
    main()
