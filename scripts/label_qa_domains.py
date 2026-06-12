#!/usr/bin/env python3
"""给全量 QA 打业务域标签（写入 qa_item.domains），供域路由缩小候选池用。

用法（在 backend/ 目录、激活 venv 后跑）：
    .venv/bin/python ../scripts/label_qa_domains.py --dry-run        # 只看分类结果，不写库
    .venv/bin/python ../scripts/label_qa_domains.py --only-empty     # 只给还没标的 QA 打标
    .venv/bin/python ../scripts/label_qa_domains.py                  # 全量重打（覆盖）
    .venv/bin/python ../scripts/label_qa_domains.py --limit 20       # 只处理前 20 条（试跑）

设计：
- 复用 app.services.domain_router.DOMAINS 的域清单（改域只改一处）。
- 逐条调 LLM 分类「问题+答案」→ 1~3 个域，写回 domains 字段（逗号分隔）。
- 分类失败/返回空 → 该条留空（matcher 侧未分类 QA 永远参与匹配，不会被漏掉）。
- 写库后 bump qa_store 版本让 matcher 缓存失效（reload）。
"""
from __future__ import annotations
import sys
import json
import re
import time
import argparse
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
from app.db import SessionLocal, QaItem  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import runtime_settings  # noqa: E402
from app.services.domain_router import DOMAINS, _VALID  # noqa: E402
from app.services.qa_store import store  # noqa: E402

runtime_settings.load_from_db()


def _domains_brief() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in DOMAINS.items())


SYS_MSG = (
    "你是企业 QA 知识库的分类器。下面给你一条知识库 QA（问题+标准答案），"
    "把它归类到一个或多个业务域（最多 3 个）。\n\n"
    "【业务域清单】\n" + _domains_brief() + "\n\n"
    "【规则】\n"
    "1. 看 QA **实际讲的内容**归类，不要只看字面关键词。\n"
    "2. **必须至少归一个最接近的域**。即使问题措辞含糊/口语化，也要判断它在讲哪块功能：\n"
    "   - 提到「生成/出图/画质/参考图/人物图/场景图」→ image；提到「视频/镜头/出片/超分」→ video\n"
    "   - 提到「画布/节点/分镜/导演台/连线/运镜」→ canvas；提到「素材/资产/收藏/管理/套用」→ asset\n"
    "   - 提到「模型/参数/哪个更合适」→ model；提到「剧本/剧情/台词/文案/分集」→ script\n"
    "   - 「新手/怎么开始/更快完成初版/整体流程」→ onboarding\n"
    "3. 一条 QA 跨多个域就返回多个（最多 3）。\n"
    "4. **只有完全无法对应任何功能**（纯寒暄、与产品无关的废话）才返回空数组 []。这种情况应极少。\n"
    "5. 严格只输出 JSON：{\"domains\": [\"image\"], \"reason\": \"<10字以内>\"}"
)


def classify_qa(question: str, answer: str, timeout: float = 15.0) -> set:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    usr = f"问题：{(question or '')[:300]}\n答案：{(answer or '')[:500]}"
    payload = {
        "model": settings.llm_model,
        "temperature": 0.0,
        "max_tokens": 80,
        "messages": [
            {"role": "system", "content": SYS_MSG},
            {"role": "user", "content": usr},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    with httpx.Client(timeout=timeout) as cli:
        r = cli.post(url, json=payload, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return set()
    parsed = json.loads(m.group(0))
    arr = parsed.get("domains") or []
    if not isinstance(arr, list):
        return set()
    return {str(d).strip() for d in arr if str(d).strip() in _VALID}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印分类结果，不写库")
    ap.add_argument("--only-empty", action="store_true", help="只处理 domains 为空的 QA")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    ap.add_argument("--sleep", type=float, default=0.6, help="每条间隔秒数（防限频，默认 0.6）")
    ap.add_argument("--retries", type=int, default=2, help="单条失败重试次数（应对限频，默认 2）")
    args = ap.parse_args()

    if not settings.llm_enabled or not settings.llm_api_key or not settings.llm_base_url:
        print("❌ LLM 未启用或缺少 api_key/base_url，无法分类")
        sys.exit(1)

    db = SessionLocal()
    try:
        q = db.query(QaItem).filter(
            QaItem.status == "approved",
            QaItem.enabled == "1",
            QaItem.deleted == "0",
        ).order_by(QaItem.id.asc())
        items = q.all()
    finally:
        db.close()

    if args.only_empty:
        items = [it for it in items if not (getattr(it, "domains", "") or "").strip()]
    if args.limit > 0:
        items = items[:args.limit]

    print(f"=== 待处理 {len(items)} 条 QA（dry_run={args.dry_run} only_empty={args.only_empty}）===\n")

    stat = {k: 0 for k in DOMAINS}
    empty_cnt = 0
    fail_cnt = 0
    done = 0
    total = len(items)
    progress_path = "/tmp/label_qa_progress.txt"

    def _write_progress(extra=""):
        try:
            with open(progress_path, "w") as f:
                f.write(f"进度 {done}/{total}  失败 {fail_cnt}  空 {empty_cnt}  {extra}\n")
        except Exception:
            pass

    for i, it in enumerate(items, 1):
        doms = None
        last_err = None
        for attempt in range(args.retries + 1):
            try:
                doms = classify_qa(it.question or "", it.answer or "")
                last_err = None
                break
            except Exception as e:
                last_err = e
                # 限频/瞬时错误：退避后重试（1.5s, 3s, ...）
                time.sleep(1.5 * (attempt + 1))
        if last_err is not None:
            fail_cnt += 1
            done += 1
            print(f"[{i}/{total}] #{it.id} ❌ 分类失败（已重试 {args.retries} 次）：{last_err}", flush=True)
            _write_progress()
            continue
        if not doms:
            empty_cnt += 1
        for d in doms:
            stat[d] += 1
        dom_str = ",".join(sorted(doms))

        # 实时写库：每条分类完立即提交（dry-run 跳过），便于随时查进度、中断可续
        if not args.dry_run:
            db = SessionLocal()
            try:
                row = db.get(QaItem, it.id)
                if row is not None:
                    row.domains = dom_str
                    db.commit()
            finally:
                db.close()

        done += 1
        print(f"[{i}/{total}] #{it.id} {dom_str or '(空)':<24} | {(it.question or '')[:34]}", flush=True)
        _write_progress(f"最近:#{it.id} {dom_str or '空'}")
        time.sleep(args.sleep)  # 防限频

    print("\n" + "=" * 60)
    print("📊 域分布：")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:<12} {v}")
    print(f"  未归类(空)   {empty_cnt}")
    print(f"  分类失败     {fail_cnt}")

    if args.dry_run:
        print("\n[dry-run] 未写库。")
        return

    print(f"\n✅ 已实时写库 {done - fail_cnt} 条（失败 {fail_cnt} 条未写）。")

    # 让 matcher 缓存失效
    try:
        store.reload()
        print("✅ qa_store 已 reload，matcher 缓存将重建。")
    except Exception as e:
        print(f"⚠️ store.reload 失败（重启服务也会重建）：{e}")


if __name__ == "__main__":
    main()
