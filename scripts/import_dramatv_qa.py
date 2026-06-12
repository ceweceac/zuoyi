#!/usr/bin/env python3
"""
把审核过的 DramaTV gap QA 导入 DB（status=pending，等审核员通过）。

格式：data/dramatv_gap_qa_review.xlsx
  A: 来源章节
  B: 问题
  C: 答案
  D: 分类
  E: 标签
  F: 审核状态（留空=待审入库 / SKIP=跳过）

行为：
- status='pending'（必须管理员/审核员去 /qa 点【通过】才生效）
- 自动去重：DB 已有 question 完全一致 → 跳过
- 写入 created_by='import_dramatv'
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal, QaItem  # noqa: E402
from app.services.qa_store import store  # noqa: E402
import openpyxl  # noqa: E402

DEFAULT_FILE = BACKEND / "data" / "dramatv_gap_qa_review.xlsx"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    f = Path(args.file)
    if not f.exists():
        print(f"❌ {f} 不存在")
        sys.exit(1)

    wb = openpyxl.load_workbook(f)
    ws = wb.active

    db = SessionLocal()
    try:
        existing = set(q for (q,) in db.query(QaItem.question).filter(QaItem.deleted == "0").all())
        added = skipped_skip = skipped_dup = skipped_empty = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[1] or not row[2]:
                skipped_empty += 1
                continue
            title, q, a, cat, tags, status = (row + (None,) * 6)[:6]
            q, a = str(q).strip(), str(a).strip()
            if not q or not a:
                skipped_empty += 1
                continue
            if (status or "").strip().upper() == "SKIP":
                skipped_skip += 1
                continue
            if q in existing:
                skipped_dup += 1
                print(f"  ⏭️  跳过重复: {q[:40]}")
                continue
            if args.dry_run:
                print(f"  [DRY] +pending  {q[:50]} → {a[:50]}")
                added += 1
                continue
            db.add(QaItem(
                question=q, answer=a,
                category=(str(cat).strip() if cat else "DramaTV"),
                tags=(str(tags).strip() if tags else "DramaTV,来源:DramaTV使用指南"),
                status="pending", enabled="1", deleted="0", version=1,
                created_by="import_dramatv", updated_by="import_dramatv",
                created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
            ))
            existing.add(q)
            added += 1

        if not args.dry_run:
            db.commit()
            # 不触发 store.reload() — 这些是 pending 状态，要等管理员审核通过后再热更新
            print(f"\n✅ 已入库 {added} 条 (status=pending)")
        else:
            print(f"\n[DRY RUN] 会入库 {added} 条")

        print(f"\n统计：")
        print(f"  ➕ 待入库: {added}")
        print(f"  ⏭️  跳过(SKIP标记): {skipped_skip}")
        print(f"  ⏭️  跳过(已有重复): {skipped_dup}")
        print(f"  ⏭️  跳过(空内容): {skipped_empty}")
        print(f"\n下一步：浏览器进 /qa → 状态筛选「待审核」 → 逐条审 → 点【通过】")
    finally:
        db.close()


if __name__ == "__main__":
    main()
