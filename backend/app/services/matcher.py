"""轻量问题匹配：先在已审核 QA 里按关键词/相似问题找命中，命中即 A 级直接回答；
不命中再走 LLM。这样：
1. 用户问"图片质量不如官网" 命中知识库的"图片/视频生成的质量差，不如官网" → 直接返回标准答案
2. 完全没相关条目才让 LLM 兜底
"""
import re
import threading
from typing import Optional, Tuple
from sqlalchemy import select

from ..db import SessionLocal, QaItem


# ============================================================
# 进程内缓存：避免每次匹配都全表 scan + 重新分词
# 与 qa_store.store.version 联动失效（运营改 KB 后 reload 会 ++version）
# ============================================================
_cache_lock = threading.Lock()
_cache = {
    "version": -1,
    "items": [],            # [QaItem, ...]
    "tokens_main": [],      # 与 items 一一对应：主问题分词
    "tags_list": [],        # 与 items 一一对应：[[(tag_str, tag_tokens), ...], ...]
    "domains_list": [],     # 与 items 一一对应：每条 QA 的域标签 set（空 set = 未分类，永远不被过滤）
}


# 域降权系数：软过滤时，QA 域与路由域不交集 → 排序分数乘这个系数（沉底但不删除）。
# 取 0.5：足以把跨域噪声压到对题候选之后，又不会把"路由判错域"的真命中彻底埋掉。
_DOMAIN_DEMOTE = 0.5


def _parse_domains(raw: str) -> set:
    """把 domains 字段（逗号/中文逗号/顿号/竖线分隔）解析成去空的 set。"""
    if not raw:
        return set()
    s = raw.replace("，", ",").replace("、", ",").replace("|", ",")
    return {t.strip() for t in s.split(",") if t.strip()}



def _get_kb_cache():
    """返回 (items, tokens_main, tags_list, domains_list)，按 qa_store 版本失效重建。"""
    from .qa_store import store as _store
    ver = _store.version
    with _cache_lock:
        if _cache["version"] == ver and _cache["items"]:
            return (_cache["items"], _cache["tokens_main"],
                    _cache["tags_list"], _cache["domains_list"])
    # 重建（锁外做 DB IO，避免 hold lock）
    db = SessionLocal()
    try:
        items = db.execute(
            select(QaItem).where(
                QaItem.status == "approved",
                QaItem.enabled == "1",
                QaItem.deleted == "0",
            )
        ).scalars().all()
    finally:
        db.close()
    # 预分词
    tokens_main = [_tokens(_strip_meta_wrapping(it.question or "")) for it in items]
    tags_list = []
    for it in items:
        raw = (it.tags or "").replace("，", ",").replace("、", ",").replace("|", ",")
        per = []
        for t in raw.split(","):
            t = t.strip()
            if t:
                per.append((t, _tokens(t)))
        tags_list.append(per)
    domains_list = [_parse_domains(getattr(it, "domains", "") or "") for it in items]
    with _cache_lock:
        # 双检：如果其他线程已经重建过更新版本，直接用最新的
        if _cache["version"] >= ver:
            return (_cache["items"], _cache["tokens_main"],
                    _cache["tags_list"], _cache["domains_list"])
        _cache["version"] = ver
        _cache["items"] = items
        _cache["tokens_main"] = tokens_main
        _cache["tags_list"] = tags_list
        _cache["domains_list"] = domains_list
    return items, tokens_main, tags_list, domains_list


_SPLIT_RE = re.compile(r"[\s,，。？?！!、；;:：（）()\[\]【】\"'""''/\\.…—-]+")

# 通用功能词 / 高频虚词：这些 token 出现得到处都是，单靠它们匹配不算"实质命中"。
# 用途：如果用户和 QA 的交集 token 全是这些，视为 false positive，分数清零。
# 注意只过滤纯通用词，"版权""人脸""素材""充值"等实体词不在此列。
_STOP_GRAMS = frozenset({
    # 疑问/连接词
    "怎么", "什么", "如何", "为什", "为什么", "可以", "请问", "能否", "是否",
    "为啥", "咋", "咋样", "咋办",
    # 通用动词/介词
    "处理", "怎么办", "怎么处理", "解决", "操作",
    # 2-gram 残片（来自上面整词的窗口切分）
    "了怎", "么处", "权了", "什么处", "么办",
    # 称呼/语气
    "请问", "麻烦", "老师", "你好", "请帮我",
    # 人称代词
    "我的", "你的", "他的",
})

# 口语 → 书面 归一化映射：仅作用于用户输入侧（KB 内容保持原样）。
# 顺序敏感：长 key 在前，避免子串先被替换（如"咋样"必须早于"咋"）。
_COLLOQUIAL_MAP = [
    ("为啥", "为什么"),
    ("咋样", "怎么样"),
    ("咋办", "怎么办"),
    ("咋", "怎么"),
    ("啥时候", "什么时候"),
    ("啥的", "什么"),
    ("啥", "什么"),
]


def _normalize_colloquial(s: str) -> str:
    """把用户输入里的常见口语词替换成书面词，提高匹配召回。"""
    if not s:
        return s
    for k, v in _COLLOQUIAL_MAP:
        if k in s:
            s = s.replace(k, v)
    return s


def _strip_meta_wrapping(q: str) -> str:
    """剥掉"用户问 X 怎么回复"这类客服 meta 包装，提取核心问题 X。

    例：'用户问怎么做素材二次创作怎么回复？' → '怎么做素材二次创作'

    边界保护：必须**同时**有 meta 前缀（用户问…）和 meta 后缀（怎么回复 等），
    才认为这是 meta 包装。否则原样返回 —— 避免误伤"用户问得很奇怪" 这类
    "用户问"恰巧出现在问句开头的提问。
    """
    if not q:
        return q
    s = q.strip()
    has_prefix = False
    # 前缀
    for prefix in ("如果用户问到", "如果用户问", "用户问到", "用户问"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            # 同时剥掉紧跟的引号
            s = s.lstrip('「『""\'\'""')
            has_prefix = True
            break
    # 后缀（顺序：长在前）
    has_suffix = False
    for suffix in (
        "怎么回复？", "怎么回答？", "怎么回应？", "该怎么回复？",
        "怎么回复", "怎么回答", "怎么回应", "该怎么回复",
        "怎么答？", "怎么答"
    ):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            s = s.rstrip('」』""\'\'""？?')
            has_suffix = True
            break
    # 仅当前后缀都剥到了才认为是 meta，否则原文返回（避免误剥）
    if not (has_prefix and has_suffix):
        return q
    return s or q  # 剥光了用原文兜底


def _tokens(s: str):
    """中英文混合的轻量分词：去标点、转小写、按字符 + 单词混合。"""
    if not s:
        return set()
    s = s.lower()
    # 拆词
    parts = _SPLIT_RE.split(s)
    out = set()
    for p in parts:
        if not p:
            continue
        out.add(p)
        # 对中文字符串再做 2-gram 增加召回（"图片质量" -> 图片, 片质, 质量）
        if any("一" <= ch <= "鿿" for ch in p):
            for i in range(len(p) - 1):
                out.add(p[i:i + 2])
    return out


def _score(user_tokens: set, qa_text: str) -> float:
    """相似度分数 = max(dice, qa 端召回率)。

    dice 对"用户长叙事 + QA 短问题"组合不友好（分母被用户大 token 集合推大）。
    qa 端召回率 = 交集 / QA tokens，衡量"QA 的核心特征是否都出现在用户问句里"。
    取二者最大值，让长问句也能被准确分类。

    实质命中校验：如果交集 token 全部都是通用功能词（怎么/处理/什么...），
    视为 false positive 直接 0 分。例：用户"涉及版权了怎么处理" 不该匹配
    "不小心生成错了怎么处理？"——共享的只是"怎么处理"这个尾巴，"版权"才是核心。

    为防止极短 QA（如单字 tag "卡"）走 recall 路径误命中，要求 qa tokens ≥ 4 才允许 recall。
    """
    if not user_tokens or not qa_text:
        return 0.0
    qt = _tokens(qa_text)
    if not qt:
        return 0.0
    inter = user_tokens & qt
    if not inter:
        return 0.0
    # 实质命中校验：交集去掉通用功能词后，必须还有"实体词"剩下
    substantive = inter - _STOP_GRAMS
    if not substantive:
        return 0.0
    dice = 2 * len(inter) / (len(user_tokens) + len(qt))
    if len(qt) >= 4:
        recall_qa = len(inter) / len(qt)
        return max(dice, recall_qa)
    return dice


def _has_any(text: str, words) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(w in t for w in words)


# 互斥语义对：用户和 QA 各自专指一种媒介时，命中相反方的分数要严罚。
# 比如用户问"图片生成慢"不应该高分匹配到"视频生成慢"的 QA。
_MUTEX_GROUPS = [
    (("图片", "图象", "图像", "生图", "出图", "画图", "海报"),
     ("视频", "短视频", "影片", "生视频", "出视频", "视频生成", "视频超分")),
]


def _mutex_penalty(user_text: str, qa_text: str) -> float:
    """返回 1.0 = 不惩罚，<1.0 = 惩罚系数。

    若用户句明显只提到 A 类（如图片），QA 明显只提到 B 类（如视频），
    且双方不互相提到对方类，则把分数压到 0.3。
    """
    if not user_text or not qa_text:
        return 1.0
    ul = user_text.lower()
    ql = qa_text.lower()
    for side_a, side_b in _MUTEX_GROUPS:
        u_in_a = _has_any(ul, side_a)
        u_in_b = _has_any(ul, side_b)
        q_in_a = _has_any(ql, side_a)
        q_in_b = _has_any(ql, side_b)
        # 用户只说 A，QA 只说 B（反之亦然）→ 误命中，重罚
        if u_in_a and not u_in_b and q_in_b and not q_in_a:
            return 0.3
        if u_in_b and not u_in_a and q_in_a and not q_in_b:
            return 0.3
    return 1.0


def _best_score_against_qa(u_tok: set, qa_question: str, qa_tags: str, user_text: str = "") -> float:
    """
    对一条 QA 算最佳相似度：主问题 + 每个 tag 各自独立算 dice，取最高。
    主问题如果是"用户问 X 怎么回复"格式（客服 meta），自动剥成 X 再算，
    避免被'用户问'、'怎么回复'这种通用词稀释。

    user_text：原始用户问句（不分词），用于做"图/视频"互斥语义降权。
    """
    main_q = _strip_meta_wrapping(qa_question or "")
    best = _score(u_tok, main_q)
    best_text = main_q
    if qa_tags:
        # 支持中英文逗号、顿号、竖线分隔
        raw = qa_tags.replace("，", ",").replace("、", ",").replace("|", ",")
        for tag in raw.split(","):
            tag = tag.strip()
            if not tag:
                continue
            s = _score(u_tok, tag)
            if s > best:
                best = s
                best_text = tag
    if user_text and best > 0:
        best *= _mutex_penalty(user_text, best_text)
    return best


def _score_from_tokens(user_tokens: set, qa_tokens: set) -> float:
    """快速版 _score：直接收预分词的 token 集合，不再 _tokens()。"""
    if not user_tokens or not qa_tokens:
        return 0.0
    inter = user_tokens & qa_tokens
    if not inter:
        return 0.0
    substantive = inter - _STOP_GRAMS
    if not substantive:
        return 0.0
    dice = 2 * len(inter) / (len(user_tokens) + len(qa_tokens))
    if len(qa_tokens) >= 4:
        recall_qa = len(inter) / len(qa_tokens)
        return max(dice, recall_qa)
    return dice


def _best_score_cached(u_tok: set, main_tok: set, tags: list,
                       qa_main_text: str, user_text: str = "") -> float:
    """缓存版 _best_score_against_qa：用预算 token 集合算分。"""
    best = _score_from_tokens(u_tok, main_tok)
    best_text = qa_main_text
    for tag_str, tag_tok in tags:
        s = _score_from_tokens(u_tok, tag_tok)
        if s > best:
            best = s
            best_text = tag_str
    if user_text and best > 0:
        best *= _mutex_penalty(user_text, best_text)
    return best


def top_k_candidates(user_question: str, k: int = 5, prefilter_threshold: float = 0.20,
                     domain_filter: set = None) -> list:
    """
    返回 top-K 候选 QA，按 score 降序。
    用于 LLM-as-judge 场景：dice 先粗筛，再让 LLM 决定。

    domain_filter（可选）：业务域 set。**软过滤**策略——不删除任何候选（零回归），
    只把「与路由域不交集」的 QA 在排序时降权（乘 _DOMAIN_DEMOTE），让域相关候选浮到
    top-k 窗口前面、跨域噪声沉底，避免噪声把对题候选挤出 k 窗口让裁判看不到。
    **返回给上游的分数仍是原始 dice 分**（不含降权），保证 strong/low-confidence 阈值判断不受污染。
    未分类 QA（domains 为空）视为「域中立」，永不降权。
    传 None 或空 set 则退化为全量粗筛（与改动前完全一致）。
    """
    if not user_question:
        return []
    normalized = _normalize_colloquial(user_question)
    u_tok = _tokens(normalized)
    if not u_tok:
        return []

    items, tokens_main, tags_list, domains_list = _get_kb_cache()
    use_filter = bool(domain_filter)
    scored = []  # (item, real_score, rank_score)
    for it, main_tok, tags, doms in zip(items, tokens_main, tags_list, domains_list):
        s = _best_score_cached(u_tok, main_tok, tags,
                               qa_main_text=it.question or "",
                               user_text=normalized)
        if s < prefilter_threshold:
            continue
        # 软过滤：QA 有域标签且与路由域无交集 → 仅排序降权，不删除
        rank = s
        if use_filter and doms and not (doms & domain_filter):
            rank = s * _DOMAIN_DEMOTE
        scored.append((it, s, rank))
    scored.sort(key=lambda x: x[2], reverse=True)  # 按降权后的 rank 排序
    return [(it, s) for it, s, _ in scored[:k]]      # 但返回原始 dice 分


def best_match(user_question: str, threshold: float = 0.40,
               domain_filter: set = None) -> Optional[Tuple[QaItem, float]]:
    """
    在所有 approved + enabled 的 QA 中找最相似的一条。
    domain_filter（可选）：**软过滤**——不删候选，只对「与路由域不交集」的 QA 在选最优时降权，
    但返回的是命中条的原始 dice 分（阈值判断不受污染）。未分类 QA 永不降权。
    """
    if not user_question:
        return None
    normalized = _normalize_colloquial(user_question)
    u_tok = _tokens(normalized)
    if not u_tok:
        return None

    items, tokens_main, tags_list, domains_list = _get_kb_cache()
    use_filter = bool(domain_filter)
    best, best_score, best_rank = None, 0.0, 0.0
    for it, main_tok, tags, doms in zip(items, tokens_main, tags_list, domains_list):
        s = _best_score_cached(u_tok, main_tok, tags,
                               qa_main_text=it.question or "",
                               user_text=normalized)
        rank = s
        if use_filter and doms and not (doms & domain_filter):
            rank = s * _DOMAIN_DEMOTE
        if rank > best_rank:
            best, best_score, best_rank = it, s, rank

    # 短问题（< 8 字符或 token 数 < 3）更严：要 ≥ 0.5
    cleaned = re.sub(r"\s+", "", user_question or "")
    if len(cleaned) < 8 or len(u_tok) < 3:
        threshold = max(threshold, 0.5)

    if best and best_score >= threshold:
        return best, best_score
    return None
