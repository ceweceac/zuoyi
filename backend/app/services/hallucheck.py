"""幻觉检测：校验 LLM 兜底回答里提到的"模型名/产品名"是否在知识库里有据。

动机（真实事故）：用户问"图片生成慢"，LLM 兜底答"切换到 seedream 模型"，
但 seedream 这个模型在知识库里根本不存在——LLM 凭 seedance 的发音编造了它。

核心思路（Evidence-based）：
- 系统涉及的模型/产品名是一个**封闭小集合**（从 KB 语料统计仅几十个）。
- LLM 回答里出现"模型名样式"的英文 token（带连字符/数字，如 seedance-2.0、gpt-image-2.0），
  如果既不在已知模型白名单、也没在当前 KB 语料里出现过 → 判定为**疑似幻觉实体**。
- 命中幻觉 → 调用方决定降级（转人工 / 去掉该句 / 加免责），不直接发给用户。

零误报优先：宁可漏判（放过一个真幻觉），也不要把合法词误杀。所以：
- 只盯"模型名样式"（含数字或连字符，或已知模型前缀），不碰普通英文词（ai/api/id/prompt）。
- 白名单 + KB 实时语料双重兜底，两者任一命中就放行。
"""
import re
import logging
import threading

log = logging.getLogger(__name__)

# ── KB 语料缓存（小写），按 qa_store 版本失效重建，用于"有据"校验 ──
_corpus_lock = threading.Lock()
_corpus_cache = {"version": -1, "text": ""}


def get_kb_corpus_lower() -> str:
    """返回 KB(问题+答案+tags) + 业务背景 + 人设 的全量小写语料，带版本缓存。"""
    from .qa_store import store as _store
    ver = _store.version
    with _corpus_lock:
        if _corpus_cache["version"] == ver and _corpus_cache["text"]:
            return _corpus_cache["text"]
    # 锁外重建
    from ..db import SessionLocal, QaItem
    from ..config import settings
    db = SessionLocal()
    try:
        rows = db.query(QaItem).filter(
            QaItem.status == "approved", QaItem.enabled == "1", QaItem.deleted == "0"
        ).all()
        parts = [(r.question or "") + (r.answer or "") + (r.tags or "") for r in rows]
    finally:
        db.close()
    parts.append(getattr(settings, "product_background", "") or "")
    parts.append(getattr(settings, "bot_persona", "") or "")
    text = " ".join(parts).lower()
    with _corpus_lock:
        _corpus_cache["version"] = ver
        _corpus_cache["text"] = text
    return text

# 已知模型/产品名白名单（小写）。来自 KB 语料统计 + 业务确认。
# 注意：香蕉 = NanoBanana（同一图片模型的中英文叫法）。
_KNOWN_MODELS = {
    "seedance", "kling", "nanobanana", "wan2.7", "gpt-image", "gpt5.4",
    "gemini3.1", "deepseek-v4", "happyhorse", "dramatv", "dtv", "drama",
}

# 真实影视器材/镜头/画质参考名白名单（小写）。这些不是"AI 生成模型"，而是 LLM 优化
# 视频/画面 prompt 时会正当引用的真实电影器材（如 Cooke S7 镜头、ARRI ALEXA LF 机身）。
# 它们符合"模型名样式"（字母+数字，如 s7）但完全合法，不该被当幻觉拦截。
_KNOWN_GEAR = {
    "s7", "s8", "s4",          # Cooke S 系列镜头
    "alexa", "lf", "alexa35",  # ARRI ALEXA / LF / 35
    "cooke", "arri", "red", "venice", "sony",  # 器材品牌
    "imax", "35mm", "16mm", "70mm",            # 胶片规格
    "k35", "superspeed",                        # 常见镜头系列
}

# 明确禁止的错误实体名：LLM 一旦吐出这些，直接判幻觉（即使将来 KB 里误混入也拦）。
_FORBIDDEN = {"seedream"}

# "模型名样式"识别：英文字母开头，且(含数字 或 含连字符)，长度≥3。
# 例：seedance-2.0 / gpt-image-2.0 / wan2.7 / kling-v3-omni-pro。
# 普通词 ai/api/prompt/bug 不含数字也不含连字符 → 不会被当模型名，天然豁免。
_MODEL_SHAPE = re.compile(r"[a-zA-Z][a-zA-Z0-9]*(?:[.\-][a-zA-Z0-9]+)+|\b[a-zA-Z]+\d[a-zA-Z0-9.\-]*")


def _known_prefix(token: str) -> bool:
    """token 是否以某个已知模型名打头（如 seedance-2.0-fast 以 seedance 打头）。"""
    t = token.lower()
    return any(t == m or t.startswith(m) for m in _KNOWN_MODELS)


def detect(answer: str, kb_corpus_lower: str = "") -> list:
    """返回疑似幻觉实体列表（空列表 = 没检出）。

    answer: LLM 兜底生成的回答原文。
    kb_corpus_lower: 当前 KB + 业务背景的全量小写语料（用于"有据"校验）。
                     传空则只靠白名单判断。
    """
    if not answer:
        return []
    suspects = []
    seen = set()

    # 0) 先抓明确禁止名（如 seedream）——它们可能是纯字母不带数字/连字符，
    #    不会被 _MODEL_SHAPE 命中，必须单独按词边界扫。
    low = answer.lower()
    for bad in _FORBIDDEN:
        if re.search(r"(?<![a-zA-Z])" + re.escape(bad) + r"(?![a-zA-Z])", low):
            suspects.append(bad)
            seen.add(bad)

    # 1) 去掉 URL，避免把域名（alidocs.dingtalk.com）误当模型名
    no_url = re.sub(r"https?://\S+", " ", answer)

    for m in _MODEL_SHAPE.finditer(no_url):
        token = m.group(0).strip(".-")
        tl = token.lower()
        if not tl or tl in seen:
            continue
        seen.add(tl)
        # 明确禁止名 → 判幻觉（上面已抓，这里兜带连字符的变体）
        if tl in _FORBIDDEN:
            suspects.append(token)
            continue
        # 已知模型名（或其前缀）→ 放行
        if _known_prefix(tl):
            continue
        # 真实影视器材/镜头名（Cooke S7、ALEXA LF 等）→ 放行，不是 AI 模型幻觉
        if tl in _KNOWN_GEAR:
            continue
        # KB 语料里出现过 → 放行（有据）
        if kb_corpus_lower and tl in kb_corpus_lower:
            continue
        # 纯版本号/分辨率样式（2.0、720p、1080p、4k、2k）→ 不是模型名，放行
        if re.fullmatch(r"\d+(\.\d+)?[a-zA-Z]?|\d+[pkPK]", tl):
            continue
        # 看起来像域名（含点且各段是常见 TLD/字母）→ 放行（兜底，正常已被去 URL 处理）
        if re.search(r"\.(com|cn|net|org|io|cc|dingtalk|feishu)\b", tl):
            continue
        # 否则：模型名样式 + 不在白名单 + KB 无据 → 疑似幻觉
        suspects.append(token)
    return suspects
