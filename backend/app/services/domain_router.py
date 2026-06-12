"""业务域路由：用 LLM 把用户问题分类到一个/多个业务域，供 matcher 缩小候选池。

设计原则——**零回归**：
- 路由只做「缩小候选池」，绝不改命中判定阈值（0.92/0.30/judge 全不动）。
- 任何不确定（LLM 失败/超时/返回空/未启用）→ 返回空 set，调用方退化为全量匹配，
  最坏退回改动前现状，不会比现在更差。
- 一条 QA 可属多个域（多标签），未分类 QA 在 matcher 侧永远参与，避免漏召。

域清单与 scripts/label_qa_domains.py 共用，改这里要同步那边。
"""
import json
import re
import time
import logging
import threading

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# ── 路由结果缓存（带 TTL）──────────────────────────────
# 动机：temperature=0 仍有随机性，同一句话可能时而 {compliance} 时而 set()。
# 缓存命中的非空结果，短时间内复用，既稳定又省掉重复的 ~1.8s LLM 调用。
# 只缓存「非空」结果：偶发的 set()（分类失败/抽风）不写缓存，下次相同问题会重试，
# 避免把一次抽风的空结果锁定 TTL 之久。
_cache_lock = threading.Lock()
_cache: dict = {}          # norm_q -> (domains_set, expire_ts)
_CACHE_TTL = 300.0         # 5 分钟
_CACHE_MAX = 1000          # 上限，超了清理过期项


def _cache_get(key: str):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        if hit:
            _cache.pop(key, None)  # 过期清理
    return None


def _cache_put(key: str, doms: set):
    if not doms:
        return  # 不缓存空结果
    now = time.time()
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # 清理所有过期项；仍满则整体清空（简单可控，避免无界增长）
            expired = [k for k, (_, exp) in _cache.items() if exp <= now]
            for k in expired:
                _cache.pop(k, None)
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
        _cache[key] = (set(doms), now + _CACHE_TTL)


# ── 11 个业务域（已把「分镜/导演台」+「画布/节点」合并为「创作画布」）──
# key = 内部稳定标识（写进 qa_item.domains），value = 给 LLM 看的中文释义
DOMAINS = {
    "image":      "图片生成/编辑（生图、出图、改图、风格、参考图、画质、海报）",
    "video":      "视频生成/工具（生视频、出视频、超分、转场、视频时长、对口型）",
    "script":     "剧本/文案（剧本模式、分集、台词、文案、大纲、AI 写作）",
    "canvas":     "创作画布（无限画布、节点、连线、分镜、导演台、3D 镜头、运镜）",
    "asset":      "资产/素材库（素材、资产、上传、收藏、转为资产、二次创作、管理）",
    "model":      "模型/参数（模型选择、参数、采样、种子、精度、各家模型差异）",
    "account":    "账号/权限/团队（登录、账号、成员、角色、协作、空间、团队）",
    "compliance": "版权/合规/审核（版权、人脸、敏感、被拦截、审核规则、合规）",
    "fault":      "故障/性能（报错、卡住、失败、慢、闪退、加载不出、bug）",
    "export":     "导出/交付（导出、下载、保存到本地、格式、交付、分辨率）",
    "onboarding": "新手引导/通用（新手第一次用、怎么开始、套模板、整体流程、入门）",
}

_VALID = set(DOMAINS.keys())


def _domains_brief() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in DOMAINS.items())


def classify_question(user_text: str, timeout: float = None) -> set:
    """把用户问题分类成业务域 set。返回空 set 表示「不确定/不过滤」，调用方走全量匹配。

    多标签：允许返回 1~3 个域（问题常跨域）。LLM 不确定时应返回空数组。
    """
    if not user_text or not user_text.strip():
        return set()
    if not getattr(settings, "domain_router_enabled", False):
        return set()
    if not settings.llm_enabled or not settings.llm_api_key or not settings.llm_base_url:
        return set()

    # 缓存：同一句话短时间内复用上次的非空分类结果（稳定 + 省重复 LLM 调用）
    norm_q = user_text.strip().lower()
    cached = _cache_get(norm_q)
    if cached is not None:
        log.info("domain_router q=%r -> %s (cache)", user_text[:40], cached)
        return set(cached)

    sys_msg = (
        "你是企业 QA 客服的问题分类器。把用户问题归类到下面的业务域（可多选，最多 3 个）。\n\n"
        "【业务域清单】\n" + _domains_brief() + "\n\n"
        "【规则】\n"
        "1. 只输出与用户问题**直接相关**的域，宁少勿多。\n"
        "2. 问题跨多个域时可返回 2~3 个；只沾一个就返回一个。\n"
        "3. 如果无法判断、或问题太泛/与上述都不沾边，返回空数组 []（系统会走全量匹配，不会出错）。\n"
        "4. 严格只输出 JSON，无任何前后缀或 markdown：\n"
        '{"domains": ["image"], "reason": "<10字以内>"}'
    )
    usr_msg = f"用户问题：{user_text.strip()[:300]}"

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "temperature": 0.0,
        "max_tokens": 80,
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": usr_msg},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    to = timeout if timeout is not None else min(getattr(settings, "llm_timeout", 15) or 15, 8)
    t0 = time.time()
    try:
        with httpx.Client(timeout=to) as cli:
            r = cli.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            log.warning("domain_router HTTP %s: %s", r.status_code, r.text[:200])
            return set()
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        doms = _parse_domains(content)
        log.info("domain_router q=%r -> %s (%dms)", user_text[:40], doms, int((time.time() - t0) * 1000))
        _cache_put(norm_q, doms)
        return doms
    except Exception as e:
        log.warning("domain_router error: %s", e)
        return set()


def _parse_domains(content: str) -> set:
    """从 LLM 返回里容错解析出合法域 set。解析失败 → 空 set（不过滤）。"""
    if not content:
        return set()
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return set()
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return set()
    if not isinstance(parsed, dict):
        return set()
    arr = parsed.get("domains") or []
    if not isinstance(arr, list):
        return set()
    # 只保留合法域，最多 3 个；非法域被丢弃，全非法 → 空 set
    out = {str(d).strip() for d in arr if str(d).strip() in _VALID}
    return set(list(out)[:3])
