#!/usr/bin/env python3
"""失败案例回流分析（C 项）：把"手工看 badcase → 补 tag"的活半自动化。

做什么：
1. 拉最近 N 条「真·未命中 KB」的兜底/转人工对话（排除保护层、规则层、幻觉拦截等正常拦截）。
2. 前置过滤：用当前字面 matcher + 裁判重跑，**现在已能命中的跳过**（说明已修复）。
3. 对仍失败的，用**语义检索(bge-m3)**找候选——语义能找到字面找不到的对题 QA。
   （这正是语义检索的最佳定位：不进在线主流水线，做离线运维助手。）
4. 让 LLM 判断：
   - 语义高分候选其实对题 → add_tag（补 tag 让字面也能命中那条）。
   - 语义也找不到对题的 → new_qa（知识库确实缺）。
   - 本就不该走 KB → ignore。
5. 产出报告。**只读分析、不自动改库**。

依赖：需先 build_kb_index.py 建好向量索引 + ollama 跑 bge-m3。

用法（backend 目录、venv）：
  EMB_BACKEND=ollama EMB_MODEL=bge-m3 QABOT_ALLOW_WEAK_JWT=1 \
    .venv/bin/python ../scripts/failure_replay.py --limit 20 --out /tmp/failure_report.json
"""
from __future__ import annotations
import os
import sys
import json
import re
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
LAB = ROOT / "lab"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(LAB))

import httpx  # noqa: E402
from app.db import SessionLocal, Conversation  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import runtime_settings, matcher, llm as llm_svc  # noqa: E402
from app.services.qa_store import store  # noqa: E402
import semantic_search as ss  # noqa: E402

runtime_settings.load_from_db()
store.reload()

# 这些前缀说明是"保护层/规则层正常拦截"，不是知识库失败，跳过
_SKIP_RAW = ("[闲聊保护", "[空消息保护", "[挫败短反馈兜底", "[充值引导",
             "[幻觉拦截", "[LLM 投降", "[KB 命中")
# 这些是非文本/业务ID，不该走 KB（用 in 而非 startswith，兼容"我的视频 vid-xxx"这种）
_SKIP_Q_SUBSTR = ("[NON_TEXT", "vid-")


def analyze_one(q_text: str, sem_candidates: list) -> dict:
    """让 LLM 判断这条失败 case 该怎么处理。
    sem_candidates: [(rec_dict, sem_score), ...] 语义检索结果。
    """
    cand_lines = []
    for i, (rec, sc) in enumerate(sem_candidates, 1):
        cand_lines.append(
            f"[{i}] (语义{sc:.2f}) 问:{(rec.get('question') or '')[:50]} "
            f"答:{(rec.get('answer') or '')[:60]}"
        )
    cand_text = "\n".join(cand_lines) if cand_lines else "（无候选）"

    sys_msg = (
        "你是企业 QA 知识库的运维分析员。下面是一条用户问题，它**没有命中知识库**（走了兜底）。\n"
        "我用语义检索给你找了几条最接近的候选。请判断该怎么处理这条失败 case。\n\n"
        "【三选一】\n"
        "1. add_tag：候选里**有一条其实对题**（语义相近且答案能解决用户问题），\n"
        "   只是用户问法和它字面差太远没匹配上。→ 指出第几条 + 应补的 2-4 个用户真实问法 tag。\n"
        "2. new_qa：候选都不对题，知识库**确实缺这个问题**的答案。→ 建议新增。\n"
        "3. ignore：这条本就不该走知识库（闲聊/无意义/超短/在表达情绪或下指令而非提问）。→ 忽略。\n\n"
        "严格只输出 JSON（不要 markdown、不要多余文字）：\n"
        '{"action":"add_tag","cand_index":2,"tags":["问法1","问法2"],"reason":"<15字>"}\n'
        '或 {"action":"new_qa","reason":"<该补什么问题,20字>"}\n'
        '或 {"action":"ignore","reason":"<15字>"}'
    )
    usr_msg = f"用户问题：{q_text}\n\n候选 QA：\n{cand_text}"
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {"model": settings.llm_model, "temperature": 0.0, "max_tokens": 150,
               "messages": [{"role": "system", "content": sys_msg},
                            {"role": "user", "content": usr_msg}]}
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    # 带一次重试（应对偶发 parse_failed / 限频）
    for attempt in range(2):
        try:
            with httpx.Client(timeout=min(settings.llm_timeout, 20)) as cli:
                r = cli.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                time.sleep(1.0)
                continue
            content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            m = re.search(r"\{[\s\S]*\}", content)
            if m:
                return json.loads(m.group(0))
            time.sleep(0.8)
        except Exception as e:
            if attempt == 1:
                return {"action": "error", "reason": str(e)[:50]}
            time.sleep(1.0)
    return {"action": "error", "reason": "parse_failed"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="分析多少条失败 case")
    ap.add_argument("--out", default="", help="自定义输出路径；不填则自动按时间戳存到 data/failure_reports/")
    args = ap.parse_args()

    if not settings.llm_enabled or not settings.llm_api_key:
        print("❌ LLM 未启用"); sys.exit(1)

    # 语义检索后端 + 索引
    backend = ss.get_backend_from_env()
    index = ss.load_index()
    if not index:
        print("❌ 向量索引为空，先跑 build_kb_index.py"); sys.exit(1)
    print(f"语义后端: {backend.name} model={backend.model} | 索引 {len(index)} 条\n")

    db = SessionLocal()
    try:
        rows = db.query(Conversation).filter(
            Conversation.answer_level.in_(["B", "C"])
        ).order_by(Conversation.id.desc()).limit(400).all()
    finally:
        db.close()

    # 过滤出真·未命中 KB 的 case，去重
    seen, cases = set(), []
    for r in rows:
        q = (r.question or "").strip()
        raw = r.raw_llm_answer or ""
        if not q or q in seen:
            continue
        if any(sub in q for sub in _SKIP_Q_SUBSTR):
            continue
        if any(p in raw for p in _SKIP_RAW):
            continue
        seen.add(q)
        cases.append(q)
        if len(cases) >= args.limit:
            break

    print(f"=== 分析 {len(cases)} 条真·未命中 KB 的失败 case ===\n")

    report = []
    stat = {"add_tag": 0, "new_qa": 0, "ignore": 0, "error": 0, "now_fixed": 0}
    for i, q in enumerate(cases, 1):
        # 前置过滤：用当前字面 matcher + 裁判重跑，现在已能命中的跳过（已修复，不算失败）
        lit = matcher.top_k_candidates(q, k=5, prefilter_threshold=0.10)
        now_hit = None
        if lit:
            if lit[0][1] >= 0.92:
                now_hit = lit[0]
            else:
                jr = llm_svc.judge_match(q, lit)
                if jr.success and jr.choice_index >= 1 and lit[jr.choice_index - 1][1] >= 0.30:
                    now_hit = lit[jr.choice_index - 1]
        if now_hit:
            stat["now_fixed"] += 1
            print(f"[{i}] ✅ {q[:26]:28} → 现已命中 #{now_hit[0].id}({now_hit[1]:.2f})，跳过（已修复）")
            time.sleep(0.1)
            continue

        # 仍失败：用语义检索找候选
        q_vec = backend.embed_one(q)
        sem = ss.search_topk(q_vec, k=3, index=index) if q_vec else []
        res = analyze_one(q, sem)
        action = res.get("action", "error")
        stat[action] = stat.get(action, 0) + 1
        line = {"q": q, **res}
        if action == "add_tag" and sem:
            idx = res.get("cand_index", 0)
            if isinstance(idx, int) and 1 <= idx <= len(sem):
                line["target_qa_id"] = sem[idx - 1][0].get("id")
                line["target_qa_q"] = sem[idx - 1][0].get("question")
        report.append(line)
        # 控制台输出
        if action == "add_tag":
            tid = line.get("target_qa_id", "?")
            print(f"[{i}] 🏷️  {q[:26]:28} → 给 #{tid} 补 tag {res.get('tags')}  ({res.get('reason','')})")
        elif action == "new_qa":
            print(f"[{i}] ➕ {q[:26]:28} → 建议新增QA  ({res.get('reason','')})")
        elif action == "ignore":
            print(f"[{i}] ⬜ {q[:26]:28} → 忽略  ({res.get('reason','')})")
        else:
            print(f"[{i}] ❌ {q[:26]:28} → {res.get('reason','')}")
        time.sleep(0.3)

    print("\n" + "=" * 60)
    print(f"📊 汇总：补tag建议 {stat['add_tag']} | 新增QA建议 {stat['new_qa']} | 忽略 {stat['ignore']} | 已修复跳过 {stat['now_fixed']} | 失败 {stat['error']}")
    print("⚠️  这是只读分析报告，补 tag / 新增 QA 请人工确认后再操作。")

    # 报告自动持久化：默认按时间戳存到 data/failure_reports/，每次跑都留档
    if args.out:
        out_path = Path(args.out)
    else:
        report_dir = BACKEND / "data" / "failure_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        out_path = report_dir / f"report_{time.strftime('%Y%m%d_%H%M%S')}.json"
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "analyzed": len(cases),
        "stat": stat,
        "items": report,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n📄 报告已保存：{out_path}")


if __name__ == "__main__":
    main()
