"""语义检索（embedding）服务 — 影子模式。

定位：**独立工具**，不接入 pipeline.py。
作用：调研对比 matcher.py（字面匹配）vs 语义检索 谁更准，
      验证有效后再决定怎么集成进主流水线。

支持后端：
- Ollama（推荐，本地、免费、合规）：装 Ollama 后 pull bge-m3
- 通义 dashscope（备选，云端）：text-embedding-v3 / text-embedding-v2

依赖：纯 Python，不引入 numpy / sentence-transformers
向量：维度由模型决定（bge-m3 = 1024，dashscope-v3 = 1024）
相似度：cosine（纯 list 实现）
存储：JSON Lines，data/embeddings/*.jsonl

无任何对 .py 业务代码的改动。
"""
from __future__ import annotations
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

# 索引文件路径
INDEX_DIR = Path("data/embeddings")
INDEX_DIR.mkdir(parents=True, exist_ok=True)
INDEX_FILE = INDEX_DIR / "kb_vectors.jsonl"
META_FILE = INDEX_DIR / "kb_meta.json"


@dataclass
class EmbeddingBackend:
    """统一抽象：不管底层是 Ollama 还是 dashscope，对外接口一样。"""
    name: str           # "ollama" / "dashscope"
    base_url: str       # http://localhost:11434  /  https://dashscope.aliyuncs.com/compatible-mode/v1
    model: str          # bge-m3:latest  /  text-embedding-v3
    api_key: str = ""   # Ollama 任意值，dashscope 必填
    timeout: int = 30

    def embed_one(self, text: str) -> list[float] | None:
        """单条 → 向量。失败返回 None。"""
        if self.name == "ollama":
            return self._embed_ollama(text)
        if self.name == "dashscope":
            return self._embed_openai_compat([text])[0] if self._embed_openai_compat([text]) else None
        raise ValueError(f"unknown backend: {self.name}")

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """批量（dashscope 单次支持 25 条）。"""
        if self.name == "dashscope":
            results = []
            for i in range(0, len(texts), 25):
                chunk = texts[i:i + 25]
                vecs = self._embed_openai_compat(chunk)
                results.extend(vecs if vecs else [None] * len(chunk))
            return results
        # Ollama 没批量 API，逐条调
        return [self._embed_ollama(t) for t in texts]

    def _embed_ollama(self, text: str) -> list[float] | None:
        url = self.base_url.rstrip("/") + "/api/embeddings"
        try:
            r = httpx.post(url, json={"model": self.model, "prompt": text}, timeout=self.timeout)
            if r.status_code != 200:
                return None
            return r.json().get("embedding")
        except Exception:
            return None

    def _embed_openai_compat(self, texts: list[str]) -> list[list[float] | None]:
        """OpenAI 兼容 /embeddings 协议（dashscope / 大多数云端用这个）。"""
        url = self.base_url.rstrip("/") + "/embeddings"
        try:
            r = httpx.post(
                url,
                json={"model": self.model, "input": texts},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            if r.status_code != 200:
                return [None] * len(texts)
            data = r.json().get("data") or []
            data_sorted = sorted(data, key=lambda d: d.get("index", 0))
            return [d.get("embedding") for d in data_sorted]
        except Exception:
            return [None] * len(texts)


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度，纯 Python（不引入 numpy）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def get_backend_from_env() -> EmbeddingBackend:
    """从环境变量挑后端。"""
    name = os.environ.get("EMB_BACKEND", "ollama").lower()
    if name == "ollama":
        return EmbeddingBackend(
            name="ollama",
            base_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            model=os.environ.get("EMB_MODEL", "bge-m3:latest"),
            api_key=os.environ.get("OLLAMA_KEY", "ollama"),
        )
    if name == "dashscope":
        return EmbeddingBackend(
            name="dashscope",
            base_url=os.environ.get("EMB_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            model=os.environ.get("EMB_MODEL", "text-embedding-v3"),
            api_key=os.environ.get("EMB_API_KEY", ""),
        )
    raise ValueError(f"未知后端：{name}（支持 ollama / dashscope）")


# =============== 索引存储 ===============

def save_index(records: Iterable[dict]) -> int:
    """把 [{id, question, tags, vector}, ...] 写到 JSONL。"""
    n = 0
    with INDEX_FILE.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def load_index() -> list[dict]:
    if not INDEX_FILE.exists():
        return []
    out = []
    with INDEX_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def search_topk(query_vec: list[float], k: int = 5,
                index: list[dict] | None = None) -> list[tuple[dict, float]]:
    """返回 [(item_dict, score), ...] 按 score 降序。"""
    idx = index if index is not None else load_index()
    if not idx or not query_vec:
        return []
    scored = []
    for rec in idx:
        vec = rec.get("vector")
        if not vec:
            continue
        s = cosine(query_vec, vec)
        scored.append((rec, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]
