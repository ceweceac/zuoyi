#!/usr/bin/env python3
"""
DramaTV 文档 vs 现有 KB 差异分析。

逻辑：
1) 读 DramaTV 文档，按 ## / ### 标题切成小段（每段是一个知识点）
2) 对每段抽取关键词，去现有 KB 用 matcher 找命中
3) 输出 Excel：章节 / 文档摘要 / 已有 KB top1 / 评估（重复/补缺）

输出：data/dramatv_gap_analysis.xlsx，4 列：
  - section: 章节路径（如 "4.5 全能参考"）
  - excerpt: 文档内容摘要（200 字内）
  - existing_topk: 现有 KB 最相关的 3 条（问题 + 分数）
  - judgement: 自动判定（覆盖 / 部分覆盖 / 缺失）
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal, QaItem  # noqa: E402
from app.services import runtime_settings, matcher  # noqa: E402
from app.services.qa_store import store  # noqa: E402
import openpyxl  # noqa: E402

runtime_settings.load_from_db()
store.reload()  # 让 matcher 缓存最新 KB

DOC = Path("/Users/dianzhong/Downloads/DramaTV使用指南_仿LibTV结构草稿.md")
OUT = BACKEND / "data" / "dramatv_gap_analysis.xlsx"


def parse_doc(text: str) -> list[dict]:
    """按 ## / ### 标题切段。返回 [{level, title, body}, ...]"""
    sections = []
    current = None
    for line in text.split("\n"):
        m = re.match(r"^(#{2,4})\s+(.+)$", line)
        if m:
            if current:
                sections.append(current)
            level = len(m.group(1))
            title = m.group(2).strip()
            current = {"level": level, "title": title, "body": []}
        else:
            if current:
                # 跳过占位标记、纯空行
                if line.strip().startswith(("> 📷", "> 🎬", "> 📷 结果")):
                    continue
                if line.strip() == "---":
                    continue
                current["body"].append(line)
    if current:
        sections.append(current)
    return sections


def make_excerpt(body: list[str], limit: int = 250) -> str:
    """把章节正文压成短摘要，保留关键句。"""
    text = "\n".join(b for b in body if b.strip())
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def query_keywords(section: dict) -> str:
    """构造用于 KB 搜索的查询字符串：标题 + 正文首句。"""
    title = section["title"]
    # 去标题里的 emoji
    title = re.sub(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]", "", title).strip()
    # 取正文前 80 字
    body_text = "\n".join(section["body"])
    body_excerpt = body_text.strip()[:80]
    return f"{title} {body_excerpt}"


def main():
    doc_text = DOC.read_text(encoding="utf-8")
    sections = parse_doc(doc_text)
    print(f"文档共解析出 {len(sections)} 个章节")

    # 只关注 level >= 3 的细分章节（## 太粗，#### 太细）
    sections = [s for s in sections if s["level"] in (3, 4)]
    print(f"过滤后保留 {len(sections)} 个细分章节用于差异分析")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "gap_analysis"
    ws.append(["章节路径", "文档摘要", "现有 KB top1", "top1 分数",
               "现有 KB top2", "现有 KB top3", "自动判定", "建议动作"])
    for col, w in [("A", 25), ("B", 60), ("C", 35), ("D", 8),
                   ("E", 30), ("F", 30), ("G", 12), ("H", 25)]:
        ws.column_dimensions[col].width = w

    stat = {"覆盖": 0, "部分覆盖": 0, "缺失": 0}
    for sec in sections:
        title = sec["title"]
        # 跳过纯目录/占位章节
        if title in ("更新总览：", "📖 指南目录（点击标题可直接跳转到对应内容）"):
            continue
        body_text = "\n".join(b for b in sec["body"] if b.strip())
        if len(body_text.strip()) < 30:
            # 内容太少（只是导航/标题），跳过
            continue
        excerpt = make_excerpt(sec["body"])
        q = query_keywords(sec)
        topk = matcher.top_k_candidates(q, k=3, prefilter_threshold=0.10)
        top1 = top2 = top3 = ""
        top1_score = 0.0
        if topk:
            top1 = f"id={topk[0][0].id} | {topk[0][0].question[:30]}"
            top1_score = topk[0][1]
            if len(topk) > 1:
                top2 = f"id={topk[1][0].id} | {topk[1][0].question[:30]}"
            if len(topk) > 2:
                top3 = f"id={topk[2][0].id} | {topk[2][0].question[:30]}"

        # 判定逻辑
        if top1_score >= 0.50:
            judge = "覆盖"
            action = "无需新增"
        elif top1_score >= 0.25:
            judge = "部分覆盖"
            action = "可补充细节 QA"
        else:
            judge = "缺失"
            action = "🔥 重点新增"
        stat[judge] += 1

        ws.append([title, excerpt, top1, round(top1_score, 2),
                   top2, top3, judge, action])

    wb.save(OUT)
    print(f"\n✅ 输出: {OUT}")
    print(f"\n=== 差异统计 ===")
    for k, v in stat.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
