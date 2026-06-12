#!/usr/bin/env python3
"""
为全部 approved KB 建立 embedding 索引。

特点：
- 用 lab/semantic_search.py 的 EmbeddingBackend，不动主流水线
- 索引存 backend/data/embeddings/kb_vectors.jsonl
- 支持续跑（按 KB id 跳过已索引）
- 限速：每批 sleep 0.3 秒

后端选择（环境变量）：
  EMB_BACKEND=ollama         # 默认 ollama
  OLLAMA_URL=http://localhost:11434
  EMB_MODEL=bge-m3:latest

  EMB_BACKEND=dashscope      # 备选阿里云
  EMB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
  EMB_API_KEY=sk-xxx
  EMB_MODEL=text-embedding-v3

用法（在 backend 目录里跑）：
  cd backend
  # 方案1：Ollama（先 ollama pull bge-m3）
  EMB_BACKEND=ollama .venv/bin/python ../scripts/build_kb_index.py

  # 方案2：阿里云
  EMB_BACKEND=dashscope EMB_API_KEY=sk-xxx .venv/bin/python ../scripts/build_kb_index.py

  # dry-run 测连通性（只索引 5 条）
  EMB_BACKEND=ollama .venv/bin/python ../scripts/build_kb_index.py --limit 5
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
LAB = ROOT / "lab"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(LAB))

from app.db import SessionLocal, QaItem  # noqa: E402
import semantic_search as ss  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只索引前 N 条（dry-run）")
    parser.add_argument("--rebuild", action="store_true", help="重建（不跳过已索引）")
    args = parser.parse_args()

    backend = ss.get_backend_from_env()
    print(f"使用 {backend.name} @ {backend.base_url}  model={backend.model}")

    # 先探活
    test_vec = backend.embed_one("测试")
    if not test_vec:
        print(f"❌ embedding 服务连不通，检查 {backend.name} 是否在跑")
        sys.exit(1)
    print(f"✅ 探活通过，向量维度 = {len(test_vec)}")

    # 读 DB
    db = SessionLocal()
    try:
        items = db.query(QaItem).filter(
            QaItem.status == "approved",
            QaItem.deleted == "0",
        ).order_by(QaItem.id).all()
    finally:
        db.close()

    print(f"DB 共 {len(items)} 条 approved KB")

    # 续跑：读已有索引
    existing = {}
    if not args.rebuild:
        existing = {r["id"]: r for r in ss.load_index()}
        print(f"已有索引 {len(existing)} 条，跳过")

    todo = [it for it in items if it.id not in existing]
    if args.limit > 0:
        todo = todo[:args.limit]
    print(f"待索引 {len(todo)} 条")

    if not todo:
        print("没新内容要索引，退出")
        return

    # 拼 embedding 输入：question + tags（多问法增强检索覆盖）
    records = list(existing.values())  # 先保留旧的
    batch_size = 25 if backend.name == "dashscope" else 1
    t0 = time.time()

    for i in range(0, len(todo), batch_size):
        chunk = todo[i:i + batch_size]
        texts = [
            (it.question or "") + (" / " + (it.tags or "") if it.tags else "")
            for it in chunk
        ]
        vecs = backend.embed_batch(texts)
        for it, vec in zip(chunk, vecs):
            if vec is None:
                print(f"  ⚠️ id={it.id} 失败")
                continue
            records.append({
                "id": it.id,
                "question": it.question,
                "answer": it.answer,
                "tags": it.tags or "",
                "vector": vec,
            })
        if (i // batch_size) % 5 == 0:
            print(f"  [{min(i + batch_size, len(todo))}/{len(todo)}] elapsed={time.time() - t0:.0f}s")
        time.sleep(0.3)
        # 中途保存
        if (i // batch_size) % 20 == 19:
            ss.save_index(records)

    n = ss.save_index(records)
    print(f"\n✅ 索引完成：{n} 条 → {ss.INDEX_FILE}")
    print(f"   耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
