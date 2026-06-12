#!/usr/bin/env python3
"""域路由 开/关 对比验证：
1) 零回归：抽样最近真实问题，对比「关路由 vs 开路由」的 top1 候选是否一致（不该把命中的题挤掉）
2) 提精度：针对已知误命中 case（参考图敏感 被 视频慢 抢答），看开路由后跨域噪声是否被挡

用法（backend/ 下、venv）：
    .venv/bin/python ../scripts/verify_domain_router.py
"""
from __future__ import annotations
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal, Conversation  # noqa: E402
from app.services import matcher, domain_router  # noqa: E402
from app.services.qa_store import store  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import runtime_settings  # noqa: E402

runtime_settings.load_from_db()
store.reload()

# 验证脚本里临时强开路由（不写 DB，仅本进程），让 classify_question 真正走 LLM
settings.domain_router_enabled = True

prefilter = float(getattr(settings, "judge_prefilter_threshold", 0.10) or 0.10)


def topk(q, dom=None, k=3):
    return matcher.top_k_candidates(q, k=k, prefilter_threshold=prefilter, domain_filter=dom)


# ── Part 1: 零回归抽样 ──────────────────────────────────
db = SessionLocal()
try:
    rows = db.query(Conversation).order_by(Conversation.id.desc()).limit(200).all()
finally:
    db.close()

seen, samples = set(), []
for r in rows:
    q = (r.question or "").strip()
    if not q or len(q) < 3 or q in seen:
        continue
    raw = r.raw_llm_answer or ""
    if any(m in raw for m in ("[闲聊保护", "[空消息保护", "[挫败短反馈兜底")):
        continue
    seen.add(q)
    samples.append(q)
    if len(samples) >= 40:
        break

print(f"=== Part 1: 零回归抽样（{len(samples)} 条真实问题）===")
print("逐条用 LLM 路由分域，对比 关/开 路由的 top1 候选\n")

same_top1 = 0
changed = []
no_route = 0
for q in samples:
    base = topk(q)                       # 关路由（全量）
    dom = domain_router.classify_question(q)  # 真实走 LLM 分类（需开 router_enabled，下方临时强开）
    routed = topk(q, dom=dom) if dom else base
    base_top = base[0][0].id if base else None
    routed_top = routed[0][0].id if routed else None
    if not dom:
        no_route += 1
    if base_top == routed_top:
        same_top1 += 1
    else:
        changed.append((q[:30], base_top, routed_top, sorted(dom) if dom else []))

print(f"top1 一致：{same_top1}/{len(samples)}")
print(f"路由返回空（走全量，必然一致）：{no_route}")
if changed:
    print(f"\n⚠️ top1 变化的 {len(changed)} 条（需人工确认是『去噪』还是『误杀』）：")
    for q, b, r, d in changed:
        print(f"  '{q}'  关={b} → 开={r}  域={d}")
else:
    print("\n✅ 无 top1 变化——开路由对这批样本零回归。")

# ── Part 2: 已知误命中 case ─────────────────────────────
print("\n" + "=" * 60)
print("=== Part 2: 已知误命中 case ===")
cases = [
    "参考图为什么会敏感",
    "为什么我的参考图被判敏感",
    "含真人的图片为什么被拦",
]
for q in cases:
    dom = domain_router.classify_question(q)
    base = topk(q, k=5)
    routed = topk(q, dom=dom, k=5)
    print(f"\n问题：{q}")
    print(f"  路由域：{sorted(dom) if dom else '空(走全量)'}")
    print(f"  关路由 top: " + ", ".join(f"#{it.id}({s:.2f})" for it, s in base[:3]) or "无")
    print(f"  开路由 top: " + (", ".join(f"#{it.id}({s:.2f})" for it, s in routed[:3]) or "无"))
    # 标记视频慢类噪声 #999/#1221 是否被挡
    base_ids = {it.id for it, _ in base}
    routed_ids = {it.id for it, _ in routed}
    noise = {999, 1221} & base_ids
    blocked = noise - routed_ids
    if noise:
        print(f"  噪声候选 {noise} → 开路由后挡掉 {blocked or '无（未挡）'}")
