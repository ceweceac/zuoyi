"""OpenAI 兼容协议 LLM 调用网关。"""
import time
import logging
import threading
from dataclasses import dataclass

import httpx

from ..config import settings
from . import qa_store
from .qa_store import store

log = logging.getLogger(__name__)

FALLBACK = "嗯…我这边卡了下没接上，你重发一次试试，或者钉钉问下相关同事～"

# 防御：每次调 LLM 前最多每 5 秒从 DB 重读一次设置，让外部修改也能即时生效
_reload_lock = threading.Lock()
_last_reload_at: float = 0.0
_RELOAD_INTERVAL = 5.0
# 用于检测影响 system prompt 的字段是否有变化（变了 → qa_store 需要 reload）
_PROMPT_FIELDS = ("llm_system_prompt_header", "bot_persona", "product_background", "contact_routing")
_last_prompt_fingerprint: str = ""


def _maybe_reload_settings():
    """每 5 秒节流，从 DB 重读运行时设置到 settings 单例。"""
    _maybe_reload_settings_changed()


def _maybe_reload_settings_changed() -> bool:
    """同 _maybe_reload_settings，但返回布尔：影响 system prompt 的字段是否有变化。
    供 pipeline 用来决定是否同步 store.reload()。

    线程安全：加锁防止并发请求重复触发 DB 读 + settings 写。
    """
    global _last_reload_at, _last_prompt_fingerprint
    now = time.time()
    # 快速路径：未到刷新间隔，立即返回（无锁，靠 GIL 读 float 安全）
    if now - _last_reload_at < _RELOAD_INTERVAL:
        return False
    # 慢速路径：加锁防止并发穿透
    with _reload_lock:
        # 双检：进锁后再确认还没刷新过
        if now - _last_reload_at < _RELOAD_INTERVAL:
            return False
        try:
            from . import runtime_settings
            runtime_settings.load_from_db()
            _last_reload_at = now
            # 计算指纹
            fp = "|".join(str(getattr(settings, k, "") or "") for k in _PROMPT_FIELDS)
            if fp != _last_prompt_fingerprint:
                _last_prompt_fingerprint = fp
                return True
        except Exception as e:
            log.debug("reload settings failed: %s", e)
    return False


@dataclass
class LlmResult:
    text: str               # 取出的答案（或兜底话术）
    latency_ms: int
    success: bool
    url: str = ""           # 实际请求 URL
    status: int = 0         # HTTP 状态码
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error: str = ""         # 异常信息（成功时为空）


def ask(user_text: str, history: list = None, kb_context: str = None) -> LlmResult:
    """history 形如 [{'role':'user','content':...},{'role':'assistant','content':...}, ...]，按时间正序

    kb_context（RAG）：传入「与问题最相关的少量 KB 片段」时，system prompt 用
    骨架（人设+背景+联系人+格式约束）+ 这些片段，而**不发全量 678 条 KB**，大幅省 token。
    传 None（如脚本直调）→ 回退用 store.prompt（全量），保持向后兼容。
    传空串 → 用骨架 prompt（无 KB 片段），仍带人设+格式约束。
    """
    _maybe_reload_settings()
    t0 = time.time()
    if not settings.llm_enabled:
        return LlmResult(text=f"[LLM 未启用] 你说的是：{user_text}",
                         latency_ms=int((time.time() - t0) * 1000),
                         success=True, error="llm_disabled")
    url = (settings.llm_base_url or "").rstrip("/") + "/chat/completions"
    try:
        # ─── 组装 system prompt ───
        if kb_context is None:
            # 兼容旧调用：全量 KB prompt
            system_content = store.prompt or settings.llm_system_prompt_header
        else:
            # RAG：骨架 + 检索到的相关片段 + 格式约束
            parts = [qa_store.build_base_prompt()]
            if kb_context.strip():
                parts.append("=== 相关知识库 ===")
                parts.append(kb_context.strip())
                parts.append("=== 知识库结束 ===")
            parts.append("")
            parts.append("\n".join(qa_store._FORMAT_RULES))
            system_content = "\n".join(parts)
        messages = [{"role": "system", "content": system_content}]
        if history:
            messages.extend(history)
        # 在 user message 里直接夹一句格式约束（system prompt 太长容易被 LLM 忽略，
        # user message 离当前回答最近，约束力最强）
        wrapped_user = (
            f"{user_text}\n\n"
            "[回答要求] 钉钉聊天场景：禁用 markdown 符号（#/##/###/**/---/- 列表/1./2./3.），"
            "篇幅按问题复杂度来——简单问题一两句讲清，复杂或多步骤问题可适当展开把话说明白，"
            "用自然话术（先...再...），不要分点，不啰嗦、不废话结尾。\n"
            "[转人工规则] 如果你确实不知道答案、知识库里也没有相关内容：先用一句话（不超过40字）"
            "给个简单的方向或坦诚说明（比如大概可能跟什么有关、或这个你确实不太确定），"
            "然后另起一行只输出标记 <<ESCALATE>>，系统会自动把用户转给人工同事。"
            "不要让用户自己去问别人、不要长篇推测、不要编造具体规则。"
            "只要你能给出有效的操作建议或确定事实，就正常作答、不要输出这个标记。"
        )
        messages.append({"role": "user", "content": wrapped_user})

        payload = {
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "messages": messages,
            # 不再硬卡短回答；给足空间让复杂问题答完整（约 1200 中文字封顶，防失控）
            "max_tokens": 2048,
        }
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
        with httpx.Client(timeout=settings.llm_timeout) as cli:
            r = cli.post(url, json=payload, headers=headers)
        cost = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            log.warning("LLM call failed status=%s body=%s", r.status_code, r.text[:200])
            return LlmResult(text=FALLBACK, latency_ms=cost, success=False,
                             url=url, status=r.status_code, error=r.text[:500])
        data = r.json()
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        if not choices:
            return LlmResult(text=FALLBACK, latency_ms=cost, success=False,
                             url=url, status=r.status_code, error="no choices in response")
        content = (choices[0].get("message") or {}).get("content") or ""
        text = content.strip() or FALLBACK
        return LlmResult(
            text=text, latency_ms=cost, success=bool(content.strip()),
            url=url, status=r.status_code,
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            total_tokens=usage.get("total_tokens", 0) or 0,
        )
    except Exception as e:
        log.exception("LLM error: %s", e)
        return LlmResult(text=FALLBACK, latency_ms=int((time.time() - t0) * 1000),
                         success=False, url=url, error=str(e))


def rephrase(user_text: str, kb_answer: str, history: list = None) -> LlmResult:
    """用机器人人设把标准答案改写成口语化、像朋友的回复。失败/超时则原样返回 kb_answer。

    返回 LlmResult 以便上游记录 token 用量、URL、HTTP 状态、延迟等审计字段。
    success=True 表示真正调用了 LLM 并拿到内容；success=False 时 text 是兜底的 kb_answer。
    """
    _maybe_reload_settings()
    t0 = time.time()
    if not settings.llm_enabled or not settings.llm_api_key or not settings.llm_base_url:
        return LlmResult(text=kb_answer, latency_ms=0, success=False, error="llm_disabled")
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    try:
        background = (getattr(settings, "product_background", "") or "").strip()
        bg_block = (
            f"\n\n【业务背景与核心原则】\n{background}\n"
            "回答时必须先按上面的业务原则推理，再结合标准答案给出合理回应；"
            "如果用户问题与原则直接相关，可以在不偏离事实的前提下，"
            "用更贴合业务逻辑的方式补充说明，而不是机械复述标准答案。\n"
        ) if background else ""
        sys_msg = (
            (settings.bot_persona or "") + "\n\n"
            + bg_block +
            "现在用户问了一个问题，我们的知识库给出了标准答案。请你用你的口语化风格，"
            "把这条标准答案重新组织成像朋友自然聊天那样回答。要求：\n"
            "1. 内容必须忠实于标准答案与业务原则，不能改变事实，不要新增编造的内容；\n"
            "2. 只回答一段话，不要列表，不要分点；\n"
            "3. 如果之前有上下文，可以用「也、还、那」等连接词衔接；\n"
            "4. 不要透露你是 AI，不要复述用户原话；\n"
            "5. 句末不用句号（聊天习惯）。"
        )
        usr_msg = f"用户问：{user_text}\n标准答案：{kb_answer}"
        messages = [{"role": "system", "content": sys_msg}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": usr_msg})
        payload = {
            "model": settings.llm_model,
            "temperature": 0.8,
            "messages": messages,
            "max_tokens": 1024,  # 给足空间，避免润色较长的标准答案时被截断
        }
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
        with httpx.Client(timeout=min(settings.llm_timeout, 20)) as cli:
            r = cli.post(url, json=payload, headers=headers)
        cost = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            return LlmResult(text=kb_answer, latency_ms=cost, success=False,
                             url=url, status=r.status_code, error=r.text[:500])
        data = r.json()
        usage = data.get("usage") or {}
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        text = content or kb_answer
        return LlmResult(
            text=text, latency_ms=cost, success=bool(content),
            url=url, status=r.status_code,
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            total_tokens=usage.get("total_tokens", 0) or 0,
        )
    except Exception as e:
        log.exception("rephrase error: %s", e)
        return LlmResult(text=kb_answer, latency_ms=int((time.time() - t0) * 1000),
                         success=False, url=url, error=str(e))


@dataclass
class JudgeResult:
    """LLM 裁判的结果。"""
    choice_index: int = 0      # 1-based，0 表示 NONE（都不对题）
    reason: str = ""           # LLM 给出的简短理由
    latency_ms: int = 0
    success: bool = False
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


def judge_match(user_text: str, candidates: list) -> JudgeResult:
    """让 LLM 在候选 QA 列表里挑最对题的一条，或返回 NONE。

    candidates: [(QaItem, score), ...] 已按 score 降序
    返回 JudgeResult，choice_index >=1 表示选了第 N 条候选，0 表示都不对题
    """
    # 自动重载设置：如果调用方（如脚本直接调）没初始化过 settings，需要从 DB 拉一次
    _maybe_reload_settings()
    t0 = time.time()
    if not candidates:
        return JudgeResult(success=False, error="no_candidates")
    if not settings.llm_enabled or not settings.llm_api_key:
        return JudgeResult(success=False, error="llm_disabled")

    # 组装候选列表
    lines = []
    for i, (it, score) in enumerate(candidates, 1):
        q = (it.question or "").strip()[:120]
        a = (it.answer or "").strip()[:180]
        lines.append(f"[{i}] 问：{q}\n    答：{a}")
    candidates_text = "\n\n".join(lines)

    sys_msg = (
        "你是企业 QA 知识库的语义匹配裁判，**判断标准极其严格**。\n"
        "用户提了一个问题，下面是知识库里通过粗筛得到的候选 QA。\n"
        "你的任务：判断哪条 QA 的答案**真正解决了用户问题**，或者**所有候选都不对题**。\n\n"
        "【判断原则 — 必须遵守】\n"
        "1. **核心动作必须一致**：\n"
        "   - '怎么找/搜索/查看' ≠ '怎么保存/上传/导出/转为资产'\n"
        "   - '怎么处理/解决/恢复' ≠ '怎么避免/预防/规避'\n"
        "   - '什么是 X / X 是啥' ≠ '怎么用 X / X 的入口在哪'\n"
        "   - '为什么 X' ≠ '怎么解决 X'\n"
        "2. **字面词重叠不算对题**：候选问题/答案里有 '图片'、'视频'、'素材' 等关键词不代表对题，\n"
        "   必须候选**答案的实际内容**真正回应了用户**真正想知道的动作或事实**。\n"
        "3. **客服话术 meta 可以选**：候选问题是 '用户问 X 怎么回复' 这种格式时，\n"
        "   把它当作问 X 的等价 QA 来对待——它的答案就是用户想要的回复。\n"
        "4. **看真实意图，不要死抠字面**：用户问『生成要多久/多长时间』且话里带『慢、卡、太久』时，\n"
        "   真实意图是**嫌慢、想加速**，不是真要一个秒数。此时『讲清为什么慢 + 怎么加速』的答案**算对题**，\n"
        "   不要因为答案没给出具体时间数字就判 0。\n\n"
        "【宁可 NONE 也不要勉强】\n"
        "- 这是最重要的规则。如果你心里在犹豫『这条好像沾边但不完全对』，**那就是 0**。\n"
        "- 一个错的命中会让用户得到答非所问的回复，比坦诚『没找到』伤害更大。\n"
        "- 反例：用户问『怎么改为资产』，候选只有『素材怎么找』『视频怎么下载』『质量怎么优化』\n"
        "  → 这些都没回答『怎么保存/转换为资产』这个动作 → 必须返回 0\n\n"
        "【输出前自检 — 三问】\n"
        "  Q1: 这条候选的答案，是否**直接告诉了用户怎么做用户问的那件事**？\n"
        "  Q2: 还是答案只是恰好包含了用户问题里的某些名词？\n"
        "  Q3: 如果用户看到这个答案，会不会觉得『答非所问』？\n"
        "  → 任意一个回答倾向'有问题'，就返回 0。\n\n"
        "【输出格式】严格只输出 JSON，无任何前缀后缀或 markdown：\n"
        '{"choice": <编号>, "reason": "<15字以内简短理由>"}\n'
        "choice 取值：1-N 表示选第 N 条候选，0 表示都不对题。"
    )
    user_msg = f"用户问题：{user_text}\n\n候选 QA：\n{candidates_text}"

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "temperature": 0.0,        # 裁判要稳定
        "max_tokens": 150,          # 裁判只输出 JSON，不需要长篇大论
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        with httpx.Client(timeout=min(settings.llm_timeout, 15)) as cli:
            r = cli.post(url, json=payload, headers=headers)
        cost = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            return JudgeResult(success=False, latency_ms=cost,
                               error=f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        usage = data.get("usage") or {}
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

        # 容错解析 JSON
        import json, re
        # 用贪婪 + DOTALL 匹配最外层 {...}（reason 字段可能内嵌 {}，非贪婪会截断）
        m = re.search(r"\{[\s\S]*\}", content)
        parsed = None
        if m:
            # 尝试解析；如果失败（外层 { 被多余文本污染），逐步收缩到最小合法 JSON
            raw = m.group(0)
            for end in range(len(raw), 0, -1):
                if raw[end - 1] != "}":
                    continue
                try:
                    parsed = json.loads(raw[:end])
                    break
                except Exception:
                    continue
        if not isinstance(parsed, dict):
            return JudgeResult(success=False, latency_ms=cost,
                               error=f"parse_failed: {content[:120]}")

        choice = parsed.get("choice")
        reason = str(parsed.get("reason", ""))[:50]
        try:
            choice = int(choice)
        except Exception:
            choice = 0
        if choice < 0 or choice > len(candidates):
            choice = 0
        return JudgeResult(
            choice_index=choice, reason=reason, latency_ms=cost, success=True,
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            total_tokens=usage.get("total_tokens", 0) or 0,
        )
    except Exception as e:
        log.exception("judge_match error: %s", e)
        return JudgeResult(success=False, latency_ms=int((time.time() - t0) * 1000), error=str(e))


def list_models(base_url: str, api_key: str, timeout: int = 10):
    """
    调用 OpenAI 兼容协议的 GET /models，返回 (ok, models_list_or_error_text)。
    主流厂商（通义/DeepSeek/Kimi/智谱/OpenAI/vLLM）都实现了这个端点。
    Base URL 漏掉 /v1 是最常见的踩坑——404 时自动补一次重试。
    """
    if not base_url or not api_key:
        return False, "Base URL 或 API Key 为空"

    def _try(url: str):
        try:
            r = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
            return r, None
        except Exception as e:
            return None, f"网络异常：{e}"

    base = base_url.rstrip("/")
    candidates = [base + "/models"]
    # 自动兜底：URL 没带 /v1 / /v4 / /paas/v4 之类的就再试一次加 /v1
    if not any(seg in base for seg in ("/v1", "/v2", "/v3", "/v4", "/compatible-mode")):
        candidates.append(base + "/v1/models")

    last_err = None
    for url in candidates:
        r, err = _try(url)
        if err:
            last_err = err
            continue
        if r.status_code == 200:
            try:
                data = r.json()
                items = data.get("data") or data.get("models") or []
                ids = sorted({it.get("id") for it in items if isinstance(it, dict) and it.get("id")})
                return True, ids
            except Exception as e:
                return False, f"解析失败：{e}"
        last_err = f"HTTP {r.status_code} (URL={url}) : {r.text[:200]}"
    return False, last_err or "未知错误"
