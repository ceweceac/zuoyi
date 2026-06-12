#!/usr/bin/env python3
"""
把人工审核过的 tags Excel 导回 DB。

预期 Excel 格式（gen_qa_tags.py 生成的）：
  A 列: id
  B 列: 原问题
  C 列: 原答案(前60字)
  D 列: 建议tags（逗号分隔，可改）

行为：
- 只更新 qa_item.tags 字段
- 跳过 D 列为空的行（你没批准的）
- 跳过 D 列 = "SKIP" 的行（你明确不要的）
- 自动触发 qa_store.reload()，让线上 matcher 立刻吃到新 tags

用法：
  cd backend
  .venv/bin/python ../scripts/import_qa_tags.py
  .venv/bin/python ../scripts/import_qa_tags.py --dry-run   # 只看会改啥，不真改
  .venv/bin/python ../scripts/import_qa_tags.py --file path/to/edited.xlsx
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal, QaItem  # noqa: E402
from app.services.qa_store import store  # noqa: E402
import openpyxl  # noqa: E402

DEFAULT_FILE = BACKEND / "data" / "tags_review.xlsx"


def normalize_tags(raw: str) -> str:
    """逗号分隔的字符串清洗：trim、去重、过滤空、过滤过长。"""
    if not raw:
        return ""
    parts = []
    seen = set()
    for sep in [","]:
        raw = raw.replace("，", ",").replace("、", ",").replace("|", ",")
    for p in raw.split(","):
        p = p.strip().strip("「」\"'""''")
        if not p or p in seen or len(p) > 40:
            continue
        parts.append(p)
        seen.add(p)
    return ",".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="审核过的 Excel 路径")
    parser.add_argument("--dry-run", action="store_true", help="只看会改啥，不真改")
    parser.add_argument("--overwrite", action="store_true",
                        help="覆盖已有 tags（默认只补没有 tags 的）")
    args = parser.parse_args()

    f = Path(args.file)
    if not f.exists():
        print(f"❌ 文件不存在：{f}")
        sys.exit(1)

    wb = openpyxl.load_workbook(f)
    ws = wb.active
    print(f"读取 {f.name}，共 {ws.max_row - 1} 行")

    db = SessionLocal()
    try:
        updated = 0
        skipped_empty = 0
        skipped_skip = 0
        skipped_existing = 0
        not_found = 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            try:
                qa_id = int(row[0])
            except (TypeError, ValueError):
                continue

            raw_tags = (row[3] or "").strip() if len(row) >= 4 else ""

            # 跳过策略
            if not raw_tags:
                skipped_empty += 1
                continue
            if raw_tags.upper() == "SKIP":
                skipped_skip += 1
                continue

            new_tags = normalize_tags(raw_tags)
            if not new_tags:
                skipped_empty += 1
                continue

            item = db.get(QaItem, qa_id)
            if item is None:
                not_found += 1
                continue

            existing = (item.tags or "").strip()
            if existing and not args.overwrite:
                skipped_existing += 1
                continue

            if args.dry_run:
                print(f"  [DRY] id={qa_id} 旧tags={existing!r} → 新={new_tags!r}")
                updated += 1
            else:
                item.tags = new_tags
                updated += 1

        if not args.dry_run:
            db.commit()
            print(f"\n✅ 提交 {updated} 条")
            # 触发线上 qa_store 热重载
            store.reload()
            print("✅ qa_store 已热重载，新 tags 立刻生效")
        else:
            print(f"\n[DRY RUN] 会更新 {updated} 条（未提交）")

        print(f"\n统计：")
        print(f"  实际更新: {updated}")
        print(f"  跳过-D列空: {skipped_empty}")
        print(f"  跳过-SKIP: {skipped_skip}")
        print(f"  跳过-已有tags: {skipped_existing}（加 --overwrite 强制覆盖）")
        print(f"  未找到id: {not_found}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
