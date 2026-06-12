#!/usr/bin/env python3
"""
影子模式对比：用最近的真实用户问题，对比 matcher（字面）vs semantic（语义）谁更准。

逻辑：
1) 从 conversation 表拉最近 N 条用户问题
2) 对每条同时跑 matcher.top_k_candidates 和 semantic search
3) 输出 Excel：原问题 | matcher 命中 | semantic 命中 | 你的判断

用途：
- 量化语义检索对你 KB 的实际增益
- 决定是否值得真正接进 pipeline.py（届时再做改动）

用法（在 backend 目录里）：
  cd backend
  EMB_BACKEND=ollama .venv/bin/python ../scripts/shadow_compare.py --n 50
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
LAB = ROOT / "lab"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(LAB))

from app.db import SessionLocal, Conversation  # noqa: E402
from app.services import matcher  # noqa: E402
import semantic_search as ss  # noqa: E402
import openpyxl  # noqa: E402

OUTPUT = BACKEND / "data" / "shadow_compare.xlsx"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50, help="对比最近 N 条用户问题")
    parser.add_argument("--level", default="B", help="只取这个 answer_level（B=兜底，最有改进空间）")
    parser.add_argument("--topk", type=int, default=3, help="各自取 top-K")
    args = parser.parse_args()

    backend = ss.get_backend_from_env()
    print(f"语义后端: {backend.name} model={backend.model}")
    index = ss.load_index()
    if not index:
        print("❌ 索引为空，先跑 build_kb_index.py")
        sys.exit(1)
    print(f"索引：{len(index)} 条 KB")

    # 拉用户问题
    db = SessionLocal()
    try:
        q = db.query(Conversation).filter(
            Conversation.answer_level == args.level
        ).order_by(Conversation.id.desc()).limit(args.n)
        rows = q.all()
    finally:
        db.close()
    print(f"拉到 {len(rows)} 条用户问题（level={args.level}）")

    # 准备 Excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "shadow_compare"
    ws.append([
        "对话id", "用户问题",
        "matcher_top1_id", "matcher_top1_q", "matcher_top1_score",
        "semantic_top1_id", "semantic_top1_q", "semantic_top1_score",
        "matcher_top2_q", "semantic_top2_q",
        "原 bot 答案前 60 字",
    ])
    for col_w in [("A", 8), ("B", 40), ("C", 8), ("D", 30), ("E", 8),
                  ("F", 8), ("G", 30), ("H", 8), ("I", 30), ("J", 30), ("K", 40)]:
        ws.column_dimensions[col_w[0]].width = col_w[1]

    matcher_better = 0
    semantic_better = 0
    both_same = 0

    for r in rows:
        q_text = (r.question or "").strip()
        if not q_text:
            continue

        # matcher
        m_results = matcher.top_k_candidates(q_text, k=args.topk, prefilter_threshold=0.10)
        m_top1 = m_results[0] if m_results else (None, 0.0)
        m_top2 = m_results[1] if len(m_results) > 1 else (None, 0.0)

        # semantic
        q_vec = backend.embed_one(q_text)
        if q_vec:
            s_results = ss.search_topk(q_vec, k=args.topk, index=index)
        else:
            s_results = []
        s_top1 = s_results[0] if s_results else (None, 0.0)
        s_top2 = s_results[1] if len(s_results) > 1 else (None, 0.0)

        # 简单评估：如果 top1 是同一条 → both_same，不同时按谁分数高判赢
        m_id = m_top1[0].id if m_top1[0] else 0
        s_id = s_top1[0].get("id") if s_top1[0] else 0
        if m_id and s_id and m_id == s_id:
            both_same += 1
        elif m_top1[1] >= 0.5 and s_top1[1] < 0.6:
            matcher_better += 1
        elif s_top1[1] >= 0.7 and m_top1[1] < 0.4:
            semantic_better += 1

        ws.append([
            r.id, q_text,
            m_id, (m_top1[0].question[:30] if m_top1[0] else ""), round(m_top1[1], 3),
            s_id, (s_top1[0].get("question", "")[:30] if s_top1[0] else ""), round(s_top1[1], 3),
            (m_top2[0].question[:30] if m_top2[0] else ""),
            (s_top2[0].get("question", "")[:30] if s_top2[0] else ""),
            (r.answer or "")[:60],
        ])

    wb.save(OUTPUT)
    print(f"\n✅ 输出: {OUTPUT}")
    print(f"\n粗略统计：")
    print(f"  双方 top1 一致:   {both_same}")
    print(f"  matcher 明显赢:   {matcher_better}（matcher 高分且 semantic 低分）")
    print(f"  semantic 明显赢:  {semantic_better}（semantic 高分且 matcher 低分）")
    print(f"\n用 Excel 人工 review，能直观看到语义检索能不能救回 matcher 漏的问题")


if __name__ == "__main__":
    main()
