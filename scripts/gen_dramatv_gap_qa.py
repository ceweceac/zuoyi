#!/usr/bin/env python3
"""
基于 DramaTV 文档的【9 个部分覆盖章节】生成精准缺口 QA。

逻辑：
1) 读差异分析 Excel，挑出"部分覆盖"的章节
2) 对每章，从原文档抽对应段落
3) 调 LLM 严格生成 2-3 条**填补现有 KB 漏洞的**新 QA
4) 输出 Excel：让你审核后导入

LLM 严格 prompt 约束：
- 必须基于文档原文，不许编造
- 必须和现有 KB top1 有明显差异（互补，不重复）
- 答案要简洁口语化，配合佐伊人设
- 每条加 tags = "DramaTV,XXX,来源:DramaTV使用指南"

输出：data/dramatv_gap_qa_review.xlsx（5 列）
"""
from __future__ import annotations
import json
import re
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.services import runtime_settings  # noqa: E402
from app.config import settings  # noqa: E402
import httpx  # noqa: E402
import openpyxl  # noqa: E402

runtime_settings.load_from_db()

DOC = Path("/Users/dianzhong/Downloads/DramaTV使用指南_仿LibTV结构草稿.md")
GAP_XLSX = BACKEND / "data" / "dramatv_gap_analysis.xlsx"
OUT_XLSX = BACKEND / "data" / "dramatv_gap_qa_review.xlsx"


def parse_doc_sections(text: str) -> dict[str, str]:
    """返回 {title: body_text}。"""
    out = {}
    current_title = None
    current_body = []
    for line in text.split("\n"):
        m = re.match(r"^(#{2,4})\s+(.+)$", line)
        if m:
            if current_title:
                out[current_title] = "\n".join(current_body).strip()
            current_title = m.group(2).strip()
            current_body = []
        else:
            if current_title:
                if line.strip().startswith(("> 📷", "> 🎬")) or line.strip() == "---":
                    continue
                current_body.append(line)
    if current_title:
        out[current_title] = "\n".join(current_body).strip()
    return out


def load_partial_chapters() -> list[dict]:
    """从差异分析 Excel 取"部分覆盖"章节。"""
    wb = openpyxl.load_workbook(GAP_XLSX)
    ws = wb.active
    items = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        title, excerpt, top1, score, top2, top3, judge, action = row
        if judge == "部分覆盖":
            items.append({
                "title": title,
                "excerpt": excerpt or "",
                "existing_top1": top1 or "",
            })
    return items


EXTRACT_PROMPT = """你是企业知识库整理助手。下面是 DramaTV 产品使用指南的一个章节：

【章节标题】{title}

【章节原文】
{doc_text}

【现有 KB 已经有的相关 QA（避免重复）】
{existing}

任务：基于章节原文，生成 2-3 条**填补现有 KB 漏洞的**新 QA。

【硬性规则 — 必须遵守】
1. 答案必须**严格来自原文**，不能编造（包括功能名、模型名、参数）
2. 必须和"现有 KB 已经有的"**有明显差异**，不要重复
3. 问题用口语化用户视角，比如「分镜里能改运镜方向吗？」「打光支持改光源颜色吗？」
4. 答案 50-120 字，自然口语风格（不要列表 1./2./3.），用「先...再...然后」这种话术
5. 答案不要透露你是 AI，不要用「您」用「你」
6. 如果章节原文不够具体（只是占位/导航），返回空数组 []

【输出格式 — 严格 JSON，无 markdown 无前缀】
[
  {{"q": "问题1", "a": "答案1", "category": "DramaTV"}},
  {{"q": "问题2", "a": "答案2", "category": "DramaTV"}}
]"""


def call_llm(title: str, doc_text: str, existing: str, timeout: int = 60) -> list[dict]:
    if not settings.llm_enabled or not settings.llm_api_key:
        raise RuntimeError("LLM 未启用")
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    prompt = EXTRACT_PROMPT.format(
        title=title,
        doc_text=doc_text[:1500],
        existing=existing[:200],
    )
    payload = {
        "model": settings.llm_model,
        "temperature": 0.3,
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": "你只输出 JSON 数组，不要任何额外文本。"},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code != 200:
            print(f"  ⚠️ HTTP {r.status_code}: {r.text[:120]}")
            return []
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        m = re.search(r"\[[\s\S]*\]", content)
        if not m:
            print(f"  ⚠️ 无 JSON: {content[:80]}")
            return []
        arr = json.loads(m.group(0))
        return [it for it in arr if isinstance(it, dict) and it.get("q") and it.get("a")]
    except Exception as e:
        print(f"  ⚠️ 异常: {e}")
        return []


def main():
    if not GAP_XLSX.exists():
        print(f"❌ {GAP_XLSX} 不存在，先跑 dramatv_gap_analysis.py")
        sys.exit(1)
    doc_text = DOC.read_text(encoding="utf-8")
    doc_sections = parse_doc_sections(doc_text)
    print(f"文档解析: {len(doc_sections)} 个章节")

    chapters = load_partial_chapters()
    print(f"差异分析: {len(chapters)} 个部分覆盖章节")

    print(f"使用 LLM: {settings.llm_model} @ {settings.llm_base_url}")
    if not settings.llm_enabled:
        print("❌ LLM 未启用")
        sys.exit(1)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "dramatv_gap_qa"
    ws.append(["来源章节", "问题", "答案", "分类", "标签（逗号分隔，可改）", "审核状态(留空=待审/SKIP=不要)"])
    for col, w in [("A", 25), ("B", 40), ("C", 60), ("D", 12), ("E", 50), ("F", 15)]:
        ws.column_dimensions[col].width = w

    total_qa = 0
    t0 = time.time()
    for i, ch in enumerate(chapters, 1):
        title = ch["title"]
        # 章节标题里有 emoji，去 emoji 后再去文档里找
        title_clean = re.sub(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF　]", "", title).strip()
        # 找文档原文（按 title 或 title_clean 匹配）
        doc_body = doc_sections.get(title) or doc_sections.get(title_clean) or ch["excerpt"]
        if not doc_body or len(doc_body.strip()) < 30:
            print(f"[{i}/{len(chapters)}] ⏭️  {title} 文档原文太少，跳过")
            continue
        print(f"[{i}/{len(chapters)}] 抽 QA: {title}", flush=True)
        qas = call_llm(title, doc_body, ch["existing_top1"])
        for qa in qas:
            tags = "DramaTV,来源:DramaTV使用指南"
            ws.append([title, qa["q"], qa["a"], qa.get("category", "DramaTV"), tags, ""])
            total_qa += 1
        time.sleep(0.8)

    wb.save(OUT_XLSX)
    print(f"\n✅ 共生成 {total_qa} 条 QA，耗时 {time.time()-t0:.0f}s")
    print(f"   输出: {OUT_XLSX}")
    print(f"\n下一步：用 Excel 打开审核 → 跑 import_dramatv_qa.py 导回 DB（pending 状态）")


if __name__ == "__main__":
    main()
