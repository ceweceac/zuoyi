#!/usr/bin/env python3
"""
准确率回归测试：用最近真实对话，对比改 tags 前后的命中情况。

逻辑：
1) 拉最近 N 条用户问题
2) 用现在的 matcher（带新 tags）重新跑 best_match / top_k
3) 看：以前是 B 级（兜底）的，现在有多少能 A 级命中
"""
from __future__ import annotations
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal, Conversation  # noqa: E402
from app.services import matcher  # noqa: E402
from app.services.qa_store import store  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import runtime_settings  # noqa: E402

runtime_settings.load_from_db()
store.reload()

# 拉最近 100 条用户问题（去重）
db = SessionLocal()
try:
    rows = (db.query(Conversation)
            .order_by(Conversation.id.desc())
            .limit(200).all())
finally:
    db.close()

# 去重 + 排除空消息保护/闲聊保护/红线
seen = set()
samples = []
for r in rows:
    q = (r.question or "").strip()
    if not q or len(q) < 3 or q in seen:
        continue
    if "我刚才连发了几条" in q:
        continue
    raw = r.raw_llm_answer or ""
    if any(m in raw for m in ("[闲聊保护", "[空消息保护", "[挫败短反馈兜底")):
        continue
    seen.add(q)
    samples.append({
        "id": r.id, "q": q,
        "old_level": r.answer_level or "?",
        "old_escalated": r.escalated == "1",
        "old_answer": (r.answer or "")[:60],
    })
    if len(samples) >= 50:
        break

print(f"=== 用最近 {len(samples)} 条非闲聊用户提问做回归 ===\n")

# 统计
old_a = sum(1 for s in samples if s["old_level"] == "A")
old_b = sum(1 for s in samples if s["old_level"] == "B")
old_c = sum(1 for s in samples if s["old_level"] == "C")
print(f"原始分布：A 命中={old_a}  B 兜底={old_b}  C 转人工={old_c}\n")

# 重新跑 matcher
strong_th = getattr(settings, "judge_strong_threshold", 0.92)
prefilter = getattr(settings, "judge_prefilter_threshold", 0.10)

now_strong_hit = 0       # 强匹配（>=0.92）直接 A
now_weak_hit = 0         # 弱匹配（>=0.40 但 <0.92）走 judge
now_no_hit = 0           # 完全没候选
upgrade_b_to_a = 0       # B 级现在能强命中
upgrade_b_weak = 0       # B 级现在有弱命中（可能 A）
print("=" * 80)
print(f"{'对话id':<6}{'旧级别':<6}{'新 top1 分数':<14}{'判定':<10}{'命中 KB 问题':<30}")
print("=" * 80)

for s in samples:
    topk = matcher.top_k_candidates(s["q"], k=3, prefilter_threshold=prefilter)
    if not topk:
        verdict = "❌ 没候选"
        top_q = ""
        now_no_hit += 1
    else:
        item, score = topk[0]
        top_q = item.question[:28]
        if score >= strong_th:
            verdict = f"✅ 强命中 A"
            now_strong_hit += 1
            if s["old_level"] == "B":
                upgrade_b_to_a += 1
        elif score >= 0.40:
            verdict = f"🟡 弱命中"
            now_weak_hit += 1
            if s["old_level"] == "B":
                upgrade_b_weak += 1
        else:
            verdict = f"⚠️ 极弱"
            now_no_hit += 1

    print(f"{s['id']:<6}{s['old_level']:<6}{score if topk else 0:<14.2f}{verdict:<10}{top_q:<30}")

print()
print("=" * 80)
print("📊 改 tags 后的回归汇总")
print("=" * 80)
print(f"强命中（>=0.92 直接 A）:   {now_strong_hit:>3} / {len(samples)}  ({100*now_strong_hit//len(samples)}%)")
print(f"弱命中（>=0.40 走裁判）:   {now_weak_hit:>3} / {len(samples)}  ({100*now_weak_hit//len(samples)}%)")
print(f"无命中（进 LLM 兜底）:     {now_no_hit:>3} / {len(samples)}  ({100*now_no_hit//len(samples)}%)")
print()
print(f"🎉 救援效果：")
print(f"  原 B 级 → 现强命中 A：     {upgrade_b_to_a}/{old_b} 条")
print(f"  原 B 级 → 现弱命中（可能 A）：{upgrade_b_weak}/{old_b} 条")
print(f"  累计救援率：{100*(upgrade_b_to_a+upgrade_b_weak)//max(1,old_b)}% 的兜底问题现在能命中 KB")
