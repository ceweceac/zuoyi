"""
智能导入：上传文档 → 提取文本 → 切块 → 调大模型抽取 QA → 入库待审核。

支持格式：.txt / .md / .docx / .pdf / .xlsx（xlsx 走结构化导入，不调模型）
"""
from __future__ import annotations
import io
import json
import logging
import re
from typing import List, Tuple

import httpx
import openpyxl
from docx import Document as DocxDocument
import pdfplumber

from ..config import settings
from ..db import SessionLocal, QaItem
from .qa_store import store

log = logging.getLogger(__name__)


# ---------------- 文本提取 ----------------

def extract_text(filename: str, content: bytes) -> str:
    name = filename.lower()
    if name.endswith(".txt") or name.endswith(".md"):
        return content.decode("utf-8", errors="ignore")
    if name.endswith(".docx"):
        doc = DocxDocument(io.BytesIO(content))
        parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    if name.endswith(".pdf"):
        out = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    out.append(t)
        return "\n".join(out)
    raise ValueError(f"不支持的文件类型：{filename}")


# ---------------- 切块 ----------------

def chunk_text(text: str, max_chars: int = 1800, overlap: int = 200) -> List[str]:
    """按段落聚合到 ~1800 字一块，相邻块留 200 字重叠保证上下文连贯。"""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    cur = ""
    for p in paragraphs:
        if len(cur) + len(p) + 2 <= max_chars:
            cur = (cur + "\n\n" + p) if cur else p
        else:
            if cur:
                chunks.append(cur)
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars - overlap):
                    chunks.append(p[i:i + max_chars])
                cur = ""
            else:
                cur = p
    if cur:
        chunks.append(cur)
    return chunks


# ---------------- 调大模型抽取 QA ----------------

EXTRACT_PROMPT = (
    "你是企业知识库整理助手。下面是一段企业内部资料（很可能本身就是一份 QA 形式的知识库），"
    "请把其中明确写到的问答抽取出来。\n\n"
    "【硬性规则】\n"
    "1. 答案必须严格来自原文，不要编造原文没说的事实（包括人名、产品名、版本号、数字、流程）。\n"
    "2. 如果原文是 问题 | 答案 格式，按原结构抽取；同一答案对应多个用户问法时，可以分别保留。\n"
    "3. 如果一段不是问答型材料（纯目录/纯标题/空行），返回空数组 []。\n"
    "4. 仅输出 JSON 数组，每个元素含 q（问题）、a（答案）、category（分类）。\n"
    "示例输出：[{\"q\":\"如何申请\",\"a\":\"在系统提交\",\"category\":\"流程\"}]\n"
    "不要输出任何 Markdown、解释、前后缀。\n\n"
    "=== 原文 ===\n__CHUNK__\n=== 原文结束 ==="
)


def _call_llm(chunk: str, client: "httpx.Client" = None) -> List[dict]:
    """调一次 LLM 抽取 QA。
    可选传入复用的 httpx.Client，避免每次 SSL 握手；失败自动重试 2 次。
    """
    if not settings.llm_enabled or not settings.llm_api_key or not settings.llm_base_url:
        raise RuntimeError("请先在「系统设置」启用大模型并配置接口")
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model or "qwen-plus",
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不要任何额外文本。"},
            {"role": "user", "content": EXTRACT_PROMPT.replace("__CHUNK__", chunk)},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    timeout = max(settings.llm_timeout, 60)

    import time as _time
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=timeout, http2=False)
    try:
        last_err = None
        for attempt in range(3):
            try:
                r = client.post(url, json=payload, headers=headers, timeout=timeout)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}：{r.text[:200]}")
                content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                return _parse_json_array(content)
            except (httpx.RequestError, httpx.HTTPStatusError, ConnectionError, OSError) as e:
                last_err = e
                if attempt < 2:
                    _time.sleep(2 ** attempt)   # 1s, 2s 退避
                    continue
        raise RuntimeError(f"大模型连接失败（重试 3 次仍失败）：{last_err}")
    finally:
        if own_client:
            client.close()


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\[\{][\s\S]*?[\]\}])\s*```", re.M)
_JSON_INLINE = re.compile(r"(\[[\s\S]*\])")


def _parse_json_array(text: str) -> List[dict]:
    """容错解析：处理模型偶尔包 ```json ``` 或者加前后说明的情况。"""
    if not text:
        return []
    candidates = []
    m = _JSON_FENCE.search(text)
    if m:
        candidates.append(m.group(1))
    m2 = _JSON_INLINE.search(text)
    if m2:
        candidates.append(m2.group(1))
    candidates.append(text)
    for c in candidates:
        try:
            data = json.loads(c)
            if isinstance(data, list):
                return [it for it in data if isinstance(it, dict) and it.get("q") and it.get("a")]
        except Exception:
            continue
    log.warning("LLM 返回无法解析为 JSON：%s", text[:200])
    return []


# ---------------- 主入口 ----------------

def ingest(filename: str, content: bytes, created_by: str,
           progress_cb=None) -> Tuple[int, int, int, int]:
    """返回 (chunks_total, qa_extracted, qa_inserted, duplicated)"""
    text = extract_text(filename, content)
    chunks = chunk_text(text)
    total = len(chunks)
    if total == 0:
        return 0, 0, 0, 0

    extracted_all: List[dict] = []
    import time as _time
    # 复用 HTTP 连接，避免每块都做 SSL 握手 → 减少 SSL 错误
    with httpx.Client(timeout=max(settings.llm_timeout, 60), http2=False) as client:
        for i, ch in enumerate(chunks, 1):
            if progress_cb:
                progress_cb(i - 1, total, f"正在让大模型分析第 {i}/{total} 段……")
            try:
                extracted = _call_llm(ch, client=client)
            except Exception as e:
                log.exception("ingest chunk %s failed: %s", i, e)
                extracted = []
            extracted_all.extend(extracted)
            if progress_cb:
                progress_cb(i, total, f"第 {i}/{total} 段完成，累计提取 {len(extracted_all)} 条")
            _time.sleep(0.3)   # 给网关喘息

    # 去重（本批内 + 与 DB 已有）
    seen, dedup = set(), []
    bad_char = 0
    for it in extracted_all:
        q = it["q"].strip()
        # U+FFFD 防线：pdf/docx 抽取损坏字形常产出 �，绝不入库（否则会回给用户）。
        if "�" in q or "�" in (it.get("a") or ""):
            bad_char += 1
            log.warning("ingest skip item with U+FFFD: q=%r", q[:50])
            continue
        key = _norm_q(q)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(it)
    if bad_char:
        log.warning("ingest: 跳过 %d 条含损坏字符(�)的问答", bad_char)

    inserted = 0
    duplicated = 0
    revived = 0
    db = SessionLocal()
    try:
        existing = {
            _norm_q(q) for (q,) in
            db.query(QaItem.question).filter(QaItem.deleted == "0").all()
            if q
        }
        # 也建一个"已软删但 question 匹配"的索引，用于"复活"模式
        from sqlalchemy import func as _f
        deleted_map = {}
        for q_obj in db.query(QaItem).filter(QaItem.deleted == "1").all():
            deleted_map.setdefault(_norm_q(q_obj.question), q_obj)

        for it in dedup:
            key = _norm_q(it["q"])
            if key in existing:
                duplicated += 1
                continue
            # 历史软删的同名条目存在 → 复活（更新答案/分类，标记为 pending）
            if key in deleted_map:
                ghost = deleted_map[key]
                ghost.deleted = "0"
                ghost.enabled = "1"
                ghost.status = "pending"
                ghost.answer = it["a"].strip()
                ghost.category = (it.get("category") or "").strip()[:64] or None
                ghost.updated_by = created_by
                existing.add(key)
                revived += 1
                continue
            existing.add(key)
            db.add(QaItem(
                question=it["q"].strip()[:1000],
                answer=it["a"].strip(),
                category=(it.get("category") or "").strip()[:64] or None,
                status="pending",
                enabled="1", deleted="0", version=1,
                created_by=created_by, updated_by=created_by,
            ))
            inserted += 1
        db.commit()
    finally:
        db.close()
    if inserted or revived:
        store.reload()
    # inserted 包含真新增 + 复活恢复，对外语义一致："本次让 X 条进入知识库"
    return total, len(extracted_all), inserted + revived, duplicated


# ---------------- Excel 结构化导入 ----------------

def ingest_excel(content: bytes, created_by: str):
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    inserted, skipped, duplicated, revived = 0, 0, 0, 0
    header_used = {}
    db = SessionLocal()
    try:
        existing = {
            _norm_q(q) for (q,) in
            db.query(QaItem.question).filter(QaItem.deleted == "0").all()
            if q
        }
        deleted_map = {}
        for q_obj in db.query(QaItem).filter(QaItem.deleted == "1").all():
            deleted_map.setdefault(_norm_q(q_obj.question), q_obj)

        for sh in wb.worksheets:
            if sh.max_row < 2:
                continue
            header_row = [str(c.value).strip() if c.value is not None else "" for c in sh[1]]
            col = _detect_columns(header_row)
            if col["question"] is None or col["answer"] is None:
                col = {"question": 0, "answer": 1, "category": 2, "tags": 3, "alias": None}
            header_used[sh.title] = col

            for row in sh.iter_rows(min_row=2, values_only=True):
                if not row:
                    continue
                q = _safe_cell(row, col["question"])
                a = _safe_cell(row, col["answer"])
                if not q or not a:
                    skipped += 1
                    continue
                # U+FFFD 防线：含损坏字符的单元格不入库（否则会回给用户）。
                if "�" in q or "�" in a:
                    skipped += 1
                    log.warning("ingest_excel skip cell with U+FFFD: q=%r", q[:50])
                    continue
                key = _norm_q(q)
                if key in existing:
                    duplicated += 1
                    continue
                # 复活之前软删的同名条目
                if key in deleted_map:
                    ghost = deleted_map[key]
                    ghost.deleted = "0"
                    ghost.enabled = "1"
                    ghost.status = "pending"
                    ghost.answer = a
                    category = _safe_cell(row, col["category"]) or None
                    tags = _safe_cell(row, col["tags"])
                    alias = _safe_cell(row, col["alias"])
                    ghost.tags = ", ".join([x for x in (tags, alias) if x]) or None
                    if category:
                        ghost.category = category[:64]
                    ghost.updated_by = created_by
                    existing.add(key)
                    revived += 1
                    continue
                existing.add(key)
                category = _safe_cell(row, col["category"]) or None
                tags = _safe_cell(row, col["tags"])
                alias = _safe_cell(row, col["alias"])
                tags_combined = ", ".join([x for x in (tags, alias) if x]) or None
                db.add(QaItem(
                    question=q[:1000], answer=a,
                    category=(category or None) and category[:64],
                    tags=tags_combined,
                    status="pending", enabled="1", deleted="0", version=1,
                    created_by=created_by, updated_by=created_by,
                ))
                inserted += 1
        db.commit()
    finally:
        db.close()
    if inserted or revived:
        store.reload()
    return inserted + revived, skipped, duplicated, header_used


def _safe_cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    v = row[idx]
    if v is None:
        return ""
    return str(v).strip()


_HEADER_HINTS = {
    "question":  ("标准问题", "主问题", "问题", "题目", "question", "q", "Q"),
    "answer":    ("答案", "回答", "解答", "answer", "a", "A"),
    "category":  ("分类", "类别", "category"),
    "tags":      ("标签", "tag", "tags"),
    "alias":     ("相似问题", "同义问题", "别名"),
}


def _detect_columns(header_row):
    out = {k: None for k in _HEADER_HINTS}
    for idx, name in enumerate(header_row):
        clean = re.sub(r"[（(].*?[)）]", "", str(name)).strip().lower()
        for key, hints in _HEADER_HINTS.items():
            if out[key] is not None:
                continue
            for h in hints:
                if h.lower() in clean:
                    out[key] = idx
                    break
    return out


def _norm_q(q: str) -> str:
    """问题归一化：去空白、统一标点后用于判重。"""
    if not q:
        return ""
    s = q.strip().lower()
    s = re.sub(r"[?\?。！!，,\.、\s]+", "", s)
    return s


def dedupe_existing() -> int:
    """清理 DB 中按归一化问题判定的重复条目，保留 id 最小的一条。"""
    from collections import defaultdict
    removed = 0
    db = SessionLocal()
    try:
        groups = defaultdict(list)
        for it in db.query(QaItem).order_by(QaItem.id).all():
            groups[_norm_q(it.question)].append(it)
        for key, items in groups.items():
            if len(items) <= 1:
                continue
            for dup in items[1:]:
                db.delete(dup)
                removed += 1
        db.commit()
    finally:
        db.close()
    if removed:
        store.reload()
    return removed
