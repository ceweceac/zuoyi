"""完整对话编排：红线 → 投诉词 → 反复追问 → 知识库 → LLM → 敏感词 → 回填 → 分级 → 审计 → 告警。"""
import logging
from typing import List

from . import filters, llm, audit, matcher, memory, alert, domain_router, hallucheck
from . import qa_store
from .qa_store import store
from ..config import settings

log = logging.getLogger(__name__)


def _hit_complaint(text: str) -> str:
    """命中投诉词返回命中的词，否则空字符串。"""
    if not text:
        return ""
    words = [w.strip() for w in (settings.complaint_keywords or "").split(",") if w.strip()]
    for w in words:
        if w in text:
            return w
    return ""


# 用户在找人/求联系方式的表达 → 直接转人工。
# 用具体短语而非裸"联系"，避免误伤"联系上下文剧情""怎么把镜头联系起来"等正常提问。
_CONTACT_PATTERNS = (
    "怎么联系", "如何联系", "怎样联系", "联系客服", "联系人工", "联系你们",
    "联系管理员", "找客服", "找人工", "转人工", "要人工", "人工客服",
    "联系方式", "客服电话", "怎么找客服", "找谁问", "找谁咨询",
)


def _hit_contact(text: str) -> str:
    """命中"求联系/找人工"类表达返回命中的短语，否则空字符串。"""
    if not text:
        return ""
    for p in _CONTACT_PATTERNS:
        if p in text:
            return p
    return ""


def _match_recharge_rule(text: str):
    """匹配充值规则。返回 (命中的触发词, 链接, 话术) 或 None。

    recharge_rules 支持两种存储格式：
    - JSON 数组（新）：[{"keywords":"个人充值,充钱","link":"https://..","reply":"话术{link}"}, ...]
    - 行格式（旧兼容）：每行  触发词 | 链接 | 话术
    从上到下匹配，第一条命中即返回（具体场景应放前面）。
    每条规则用自己的 reply；reply 为空才回退全局 recharge_reply。
    """
    if not text:
        return None
    raw = (getattr(settings, "recharge_rules", "") or "").strip()
    if not raw:
        return None

    rules = []  # [(keywords_str, link, reply_str), ...]
    if raw.startswith("["):
        import json as _json
        try:
            arr = _json.loads(raw)
            for it in arr:
                if isinstance(it, dict):
                    rules.append((it.get("keywords", "") or "",
                                  it.get("link", "") or "",
                                  it.get("reply", "") or ""))
        except Exception:
            pass
    else:
        for line in raw.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                rules.append((parts[0], parts[1], parts[2] if len(parts) >= 3 else ""))

    default_reply = getattr(settings, "recharge_reply", "") or "充值走这个链接填一下哈 👉 {link}"
    import re as _re
    for kw_str, link, reply in rules:
        if not kw_str or not link:
            continue
        # 同时支持英文逗号 , / 中文逗号 ， / 顿号 、 三种分隔符（用户容易混用）
        for kw in _re.split(r"[,，、]", kw_str):
            kw = kw.strip()
            if kw and kw in text:
                return (kw, link, reply or default_reply)
    return None


def _hit_id_pattern(text: str) -> str:
    """命中业务 ID 模式返回完整 ID 字符串，否则空字符串。

    匹配规则：用户消息含 "{prefix}{token}" 形式，其中 token 是字母数字（≥3 个字符），
    例如 vid-10ae615ce0174b75。前缀来自 settings.escalate_id_prefixes（逗号分隔）。
    业务 ID 多半是某条具体记录/任务，标准 QA 答不上来，直接转人工最稳。
    """
    if not text:
        return ""
    prefixes_raw = getattr(settings, "escalate_id_prefixes", "") or ""
    prefixes = [p.strip() for p in prefixes_raw.split(",") if p.strip()]
    if not prefixes:
        return ""
    import re as _re
    for p in prefixes:
        # 转义前缀（防止 . / + 等被当成正则元字符）
        m = _re.search(_re.escape(p) + r"[A-Za-z0-9_]{3,}", text)
        if m:
            return m.group(0)
    return ""


# 防抖合并器（debouncer.py）会把连发的多条消息拼成
# "我刚才连发了几条，你一起看：\n1. xxx\n2. yyy"
# 如果用户那些消息全是空的（典型场景：群里 @ 机器人但没说话），
# 拼出来的就是模板头 + 空编号列表。这种情况下不应该让 LLM 兜底，
# 因为 LLM 看到空意图会用最近的对话历史乱发挥。
_DEBOUNCE_HEADER = "我刚才连发了几条，你一起看："


def _is_empty_intent(raw_text: str) -> bool:
    """判断用户消息是否为"空意图"：纯空 / 只剩防抖模板 / 仅含 @ 标记残留等。"""
    if not raw_text:
        return True
    s = raw_text.strip()
    # 剥掉防抖合并模板头
    if s.startswith(_DEBOUNCE_HEADER):
        s = s[len(_DEBOUNCE_HEADER):].strip()
        # 剥掉每行的 "N. " 前缀和 \n
        import re as _re
        s = _re.sub(r"^\s*\d+\.\s*", "", s, flags=_re.M).strip()
    # 剥掉钉钉 @ 标记残留（钉钉客户端发来的 text 一般不含 @，但保险处理）
    s = s.replace("@", "").strip()
    # 全部去掉后，长度 < 2 字符就视为空意图
    return len(s) < 2


# 产品域关键词：用户问到这些词，意味着是和产品/业务相关的提问，
# KB 未命中时不能让 LLM 自由发挥（会编造功能、参数、流程），应该转人工。
# 闲聊/通用问题（如"今天天气"、"几点了"）才允许走 LLM 兜底闲聊。
_PRODUCT_DOMAIN_KEYWORDS = (
    # 强业务/合规词：这些必须人工核实，LLM 不能编
    "授权", "版权", "合规", "红线", "拦截", "驳回",
    "投放", "成本", "费用", "充值", "权限", "账号",
    # 产品名（按你的实际产品名补充）
    "seedance", "seedream",
)


def _is_product_question(text: str) -> bool:
    """判断用户提问是否属于产品/业务域。

    用于 KB 未命中时的分流：产品域问题 → 转人工（不让 LLM 编造）；
    非产品域（闲聊、通用知识）→ 走 LLM 兜底。
    """
    if not text:
        return False
    s = text.lower().strip()
    if len(s) < 2:
        return False
    return any(kw in s for kw in _PRODUCT_DOMAIN_KEYWORDS)


# 闲聊 / 打招呼 / 表情包：用户发这些话没具体意图，不能让 LLM 用历史上下文乱发挥。
# 命中 → 用人设话术直接寒暄，不调 LLM。
# 顺序：长 key 在前避免子串先命中（如"再见"在"见"前）
_CHIT_CHAT_PATTERNS = {
    # 打招呼
    "你好", "您好", "hi", "hello", "嗨", "在吗", "在不在", "在么", "早上好", "中午好", "下午好", "晚上好",
    # 致谢 / 客气
    "谢谢", "感谢", "辛苦了", "辛苦", "多谢", "ok", "好的", "好嘞", "收到", "明白", "知道了", "了解",
    # 告别
    "拜拜", "再见", "88", "晚安", "拜",
    # 应答
    "嗯", "嗯嗯", "嗯哼", "哦", "哦哦", "啊", "啊哈", "哈哈", "哈哈哈",
}

# 钉钉表情包通常是 [表情名] 这种格式
_EMOTICON_PATTERN = None


def _is_chit_chat(raw_text: str) -> str:
    """识别闲聊/打招呼/表情包，返回命中类型（greeting/thanks/farewell/ack/emoji），否则空字符串。"""
    if not raw_text:
        return ""
    s = raw_text.strip().lower()
    import re as _re
    # 身份问题先前置检查（"what is your name?" 长 17 字符也要识别）
    # 不能走 LLM（豆包/通义等会答它自己的训练身份），必须用 bot_persona 配置的名字硬编码回答
    identity_patterns = (
        "你叫什么", "你叫啥", "你是谁", "你叫", "你的名字",
        "怎么称呼", "你叫啥名", "啥名字", "你是什么",
        "what is your name", "who are you", "what's your name", "whats your name",
    )
    if any(p in s for p in identity_patterns):
        return "identity"
    # 长消息肯定不是闲聊（>10 字符基本是有意图的问题）
    if len(s) > 10:
        return ""
    # 1. 纯表情包：钉钉表情形如 [拜托] [微笑] [大笑]
    if _re.fullmatch(r"\[[^\[\]]{1,8}\]", s):
        return "emoji"
    # 2. 标准闲聊词
    if s in _CHIT_CHAT_PATTERNS:
        # 简单分类
        if s in ("你好", "您好", "hi", "hello", "嗨", "在吗", "在不在", "在么",
                 "早上好", "中午好", "下午好", "晚上好"):
            return "greeting"
        if s in ("谢谢", "感谢", "辛苦了", "辛苦", "多谢"):
            return "thanks"
        if s in ("拜拜", "再见", "88", "晚安", "拜"):
            return "farewell"
        return "ack"
    # 3. 单纯标点 / 短叹词
    if _re.fullmatch(r"[\s\.\?!。？！~～]+", s):
        return "ack"
    return ""


def _get_bot_name() -> str:
    """从 bot_persona 的第一句话里提取 bot 名字。"""
    persona = (settings.bot_persona or "").strip()
    if not persona:
        return "助手"
    import re as _re
    m = _re.match(r"你叫\s*([^\s，,。.的]+)", persona)
    if m:
        return m.group(1).strip()
    m = _re.match(r"你是\s*([^\s，,。.的]+)", persona)
    if m:
        return m.group(1).strip()
    return "助手"


def _chit_chat_reply(kind: str) -> str:
    """根据闲聊类型返回简短人设话术。"""
    if kind == "identity":
        name = _get_bot_name()
        return f"我叫{name}，部门里的同事，平时帮大家答日常问题。有事儿你直接问～"
    table = {
        "greeting": "嗨～有事儿你直说，我能帮上的尽量帮",
        "thanks":   "客气啥，有问题继续来",
        "farewell": "拜～有事儿随时找我",
        "ack":      "嗯嗯",
        "emoji":    "嗯嗯，有问题直接说哈～",
    }
    return table.get(kind, "嗯嗯")


# 用户挫败感 / 纠错信号词：用户说了这种话 = 上一轮没答到点上
# 注意顺序：长 key 必须排在短 key 前（如"还是不行"在"不行"前），否则总命中短的丢失更精准的提示
_FRUSTRATION_WORDS = (
    # 否定型陈述
    "不对", "不是这个", "不是这", "没说这", "没问这", "没有说", "没有问", "答非所问",
    "答错", "回答错", "回答的不对", "回答不对", "你错了",
    "不太对", "不准确", "不是这个意思", "不对吧", "错的", "错了", "错啦",
    "听不懂", "看不懂", "不明白", "没明白",
    "我说的是", "我说我", "我问的是", "我要的是", "我是说",
    "再说一遍", "重新", "你听清", "你看清",
    # 口语挫败词 — 用户用更生活化的方式表达"还是没解决"
    "还是不行", "也不行", "都不行", "依然不行", "仍然不行", "搞不定", "解决不了",
    "没用", "白说", "没有用", "解不出", "处理不了",
)


def _hit_frustration(text: str) -> str:
    """命中用户挫败感关键词返回命中的词，否则空字符串。"""
    if not text:
        return ""
    for w in _FRUSTRATION_WORDS:
        if w in text:
            return w
    return ""


def _count_recent_frustration(sender: str, window_minutes: int = 5) -> int:
    """看这个用户最近 N 分钟内已经说过几次挫败感词。"""
    if not sender:
        return 0
    from ..db import SessionLocal, Conversation
    from sqlalchemy import desc
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        rows = (
            db.query(Conversation)
            .filter(Conversation.sender == sender)
            .order_by(desc(Conversation.id))
            .limit(15)
            .all()
        )
    finally:
        db.close()
    n = 0
    for r in rows:
        if not r.created_at or (now - r.created_at) > timedelta(minutes=window_minutes):
            break
        if _hit_frustration(r.question or ""):
            n += 1
    return n


def _count_recent_messages(sender: str, window_minutes: int = 5) -> int:
    """看这个用户最近 N 分钟内发过几条消息（不管主题）。"""
    if not sender:
        return 0
    from ..db import SessionLocal, Conversation
    from sqlalchemy import desc
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        rows = (
            db.query(Conversation)
            .filter(Conversation.sender == sender)
            .order_by(desc(Conversation.id))
            .limit(20)
            .all()
        )
    finally:
        db.close()
    n = 0
    for r in rows:
        if not r.created_at or (now - r.created_at) > timedelta(minutes=window_minutes):
            break
        n += 1
    return n


def _count_recent_unsolved_messages(sender: str, window_minutes: int = 2) -> int:
    """
    看用户最近 N 分钟内"没被解决"的消息条数。
    没解决 = answer_level 是 B（LLM 兜底）或 C（已转人工）。
    A 级（命中知识库标准答案）说明 bot 答对了，不计入"没解决"。

    **不计入未解决**的 B 级类型（这些是友好寒暄/已答对，不是 LLM 答不上来）：
    - [闲聊保护] 打招呼/致谢/告别/应答/表情包
    - [空消息保护] 用户没说话
    - [挫败短反馈兜底] 用户说"不对"等无意图反馈
    - [充值引导] 命中充值规则、已发对应文档链接（答对了，且已单独告警）

    用于 burst 兜底判断：用户连发 ≥ 2 条 bot 真没答好 → 转人工。
    避免"用户熟悉后快速连问不同问题、bot 都答对了"被误判为没解决。
    """
    if not sender:
        return 0
    from ..db import SessionLocal, Conversation
    from sqlalchemy import desc
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        rows = (
            db.query(Conversation)
            .filter(Conversation.sender == sender)
            .order_by(desc(Conversation.id))
            .limit(20)
            .all()
        )
    finally:
        db.close()
    n = 0
    # 这些 raw_llm_answer 标记意味着 bot 实际答得很好，不是真"没解决"
    # 注意：[充值引导] 命中后已单独触发告警群提醒人工跟进，再计入 burst
    # 会导致用户连问几次充值就被二次强制转人工（明明每次都答对了）。
    soft_b_markers = ("[闲聊保护", "[空消息保护", "[挫败短反馈兜底", "[充值引导")
    for r in rows:
        if not r.created_at or (now - r.created_at) > timedelta(minutes=window_minutes):
            break
        # C 级（已转人工）不再计入：每条 C 都已经独立触发过告警，
        # 再把它当成"未解决"会让 burst 兜底反复滚雪球（典型如产品域
        # KB 未命中直连转人工后，后续每条新消息都被叠加成"2 分钟内 N 条未解答"）。
        if (r.answer_level or "") == "B":
            # 闲聊保护/空消息保护/挫败短反馈兜底 → bot 答得很好，不计入"未解决"
            raw = (r.raw_llm_answer or "")
            if any(m in raw for m in soft_b_markers):
                continue
            n += 1
    return n


def _count_recent_unresolved(sender: str, current_text: str) -> int:
    """
    判断用户是否在连续追问同一类问题。规则：
    - 取最近 N 条对话
    - 时间间隔在 5 分钟内的算连续
    - 当前问题与历史问题有词级重叠（不限定 C 级），都视为"同一话题持续追问"
    """
    if not sender:
        return 0
    from ..db import SessionLocal, Conversation
    from sqlalchemy import desc
    from datetime import datetime, timedelta

    db = SessionLocal()
    try:
        rows = (
            db.query(Conversation)
            .filter(Conversation.sender == sender)
            .order_by(desc(Conversation.id))
            .limit(8)
            .all()
        )
    finally:
        db.close()

    # 当前问题的 token 集合
    cur_tokens = _tokens(current_text)
    if not cur_tokens:
        return 1

    now = datetime.utcnow()
    unresolved = 1   # 当前这条算一次
    for r in rows:
        # 时间窗口 5 分钟外，停止
        if not r.created_at or (now - r.created_at) > timedelta(minutes=5):
            break
        hist_tokens = _tokens(r.question or "")
        if not hist_tokens:
            break
        # 用 Jaccard 相似度（双向）判断是不是同主题
        inter = len(cur_tokens & hist_tokens)
        union = len(cur_tokens | hist_tokens)
        sim = inter / union if union else 0
        if sim >= 0.3:
            unresolved += 1
        else:
            # 一旦遇到不同主题就停（说明用户已经转话题，前面那些不算）
            break
    return unresolved


def _tokens(s: str) -> set:
    """中英文 token + 2-gram，用于相似度判断。"""
    import re
    if not s:
        return set()
    s = s.lower()
    parts = re.split(r"[\s,，。？?！!、；;:：（）()\[\]【】\"'""''/\\.…—-]+", s)
    out = set()
    for p in parts:
        if not p:
            continue
        out.add(p)
        if any("一" <= ch <= "鿿" for ch in p):
            for i in range(len(p) - 1):
                out.add(p[i:i + 2])
    return out


def _pick_handler(user_text: str) -> str:
    """根据用户问题从 contact_routing 里挑最相关的一行，作为联系人提示。"""
    routing = (settings.contact_routing or "").strip()
    if not routing:
        return ""
    best_line = ""
    best_hits = 0
    for line in routing.split("\n"):
        line = line.strip()
        if not line or "：" not in line and ":" not in line:
            continue
        sep = "：" if "：" in line else ":"
        keys, _, _ = line.partition(sep)
        # 关键词命中计数
        hits = sum(1 for k in keys.replace("/", "、").replace(",", "、").split("、") if k.strip() and k.strip() in user_text)
        if hits > best_hits:
            best_hits = hits
            best_line = line
    return best_line or routing.split("\n")[0]  # 没匹配上就用第一行兜底


def _build_user_reply(user_text: str) -> tuple:
    """生成给用户的转人工话术，返回 (reply, handler_line)。"""
    handler = _pick_handler(user_text)
    # 从联系人行里提取"人名"——冒号后的部分
    handler_name = handler
    for sep in ("：", ":"):
        if sep in handler:
            handler_name = handler.split(sep, 1)[1].strip()
            break
    tpl = settings.escalate_user_reply or "这个我帮你转给 {handler}，稍后会联系你哈～"
    return tpl.replace("{handler}", handler_name), handler


def _do_escalate(rec, sender: str, sender_name: str, raw_text: str,
                 reason: str, level: str = "warn", extra_reply: str = "",
                 prefix_reply: str = "") -> str:
    """统一的转人工：写审计 + 发告警 + 返回给用户的话术。

    extra_reply: 可选，追加到给用户的转人工话术后（如 vid- 的填写链接引导）。
    prefix_reply: 可选，放在转人工话术**前面**（如 LLM 投降时先给的一句简单回答/方向）。
    """
    user_reply, handler_line = _build_user_reply(raw_text)
    if prefix_reply:
        user_reply = prefix_reply.strip() + "\n\n" + user_reply
    if extra_reply:
        user_reply = user_reply + "\n\n" + extra_reply
    rec.answer = user_reply
    rec.answer_level = "C"
    rec.escalated = True
    conv_id = audit.write(rec)
    # 只取本次"连续对话"内的真实轮数，不固定塞 5 轮
    first_question, recent_dialog = _format_recent_dialog(sender, raw_text)
    try:
        alert.send_alert(
            level=level,
            title=reason,
            user_text=first_question,    # 用户最初的提问（真实原话）
            sender_name=sender_name,
            sender_id=sender,
            bot_reply=user_reply,
            reason=reason,
            handler_hint=handler_line,
            conversation_id=conv_id,
            recent_dialog=recent_dialog,
            issue_summary="",            # 不再用 AI 总结（容易误导，已撤销）
            at_all=True,
        )
    except Exception as e:
        log.exception("alert dispatch failed: %s", e)
    return user_reply


def _format_recent_dialog(sender: str, current_text: str, gap_minutes: int = 2) -> tuple:
    """
    返回 (first_question, dialog_md)：
      first_question  本次"连续会话"中用户最早问的内容（真实原话）
      dialog_md       格式化好的对话历史
    "连续会话" = 相邻两条间隔 ≤ gap_minutes 分钟。
    默认 2 分钟，避免几分钟前的别的话题混进来。
    """
    if not sender:
        return current_text, f"**👤 用户：** {current_text}"

    from ..db import SessionLocal, Conversation
    from sqlalchemy import desc
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        # 拉最近 20 条，按时间倒序
        rows = (
            db.query(Conversation)
            .filter(Conversation.sender == sender)
            .order_by(desc(Conversation.id))
            .limit(20)
            .all()
        )
    finally:
        db.close()

    # 从最近一条往前扫，相邻间隔 > gap_minutes 就停（之前的不属于"本次会话"）
    valid = []
    last_time = now
    for r in rows:
        if not r.created_at:
            continue
        # 间隔太大 → 前面那些不算同一轮
        if (last_time - r.created_at) > timedelta(minutes=gap_minutes):
            break
        valid.append(r)
        last_time = r.created_at
    valid.reverse()   # 现在按时间正序

    # 用户最初的提问 = 这一轮里最早的；都没有就用当前这条
    first_q = ""
    for r in valid:
        q = (r.question or "").strip()
        if q:
            first_q = q
            break
    if not first_q:
        first_q = current_text

    # 渲染（每条独立一段，钉钉 markdown 用 \n\n 才换行）
    bot_name = _get_bot_name()
    lines = []
    for r in valid:
        t = r.created_at.strftime("%H:%M")
        q = (r.question or "").strip()
        a = (r.answer or "").strip()
        if q:
            lines.append(f"**[{t}] 👤 用户：** {q}")
        if a:
            short = a[:80] + ("…" if len(a) > 80 else "")
            lines.append(f"**🤖 {bot_name}：** {short}")
    dialog_md = "\n\n".join(lines)
    return first_q, dialog_md


def _summarize_issue(sender: str, current_text: str, dialog: str) -> str:
    """[已废弃] AI 总结用户问题。注释撤销原因：易误导人工，已从 _do_escalate 移除调用。

    保留函数签名仅为兼容历史 import；新代码不要调用。
    """
    return ""


def handle(sender: str, raw_text: str, sender_name: str = "") -> str:
    raw_text = (raw_text or "").strip()
    # 每次处理前热重载运行时设置（rephrase 开关、关键词、提示词等），
    # 避免管理员在 /settings 改完后必须重启服务才生效。
    # llm.py 已有 5 秒节流，重复调用代价极低。
    from . import llm as _llm
    settings_changed = _llm._maybe_reload_settings_changed()
    # 如果 system_prompt_header / bot_persona / product_background 等影响 system prompt 的字段变了，
    # 同步重建 qa_store.prompt（否则 LLM 兜底仍用旧 prompt）
    if settings_changed:
        store.reload()
    rec = audit.AuditRecord(
        sender=sender, sender_name=sender_name,
        question=raw_text, question_desensitized=raw_text,
        llm_model=settings.llm_model or "",
        qa_prompt_version=store.version,
    )

    # 0.前) 非文本消息（图片/语音/视频/文件）→ 直接转人工
    # bot.py 检测到这类消息时会传 [NON_TEXT:xxx] 前缀标记
    if raw_text.startswith("[NON_TEXT:"):
        # 解析媒体类型
        m_type = "媒体"
        try:
            m_type = raw_text.split(":", 1)[1].split("]", 1)[0]
        except Exception:
            pass
        type_label = {
            "image": "图片", "audio": "语音", "video": "视频",
            "file": "文件", "richText": "富文本"
        }.get(m_type, m_type or "媒体")
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason=f"用户发了{type_label}，机器人无法识别，转人工查看",
                            level="warn")

    # 0) 空消息保护：群里 @ 机器人没说话，或防抖只合并了空消息，直接友好提示
    # 不调 LLM，避免它用历史对话上下文胡乱发挥
    # 注意 _is_empty_intent 也要剥掉防抖合并模板的固定前缀
    if _is_empty_intent(raw_text):
        reply = "你想问啥？说说看～"
        rec.answer = reply
        rec.answer_level = "B"
        rec.raw_llm_answer = "[空消息保护] 用户消息为空或只有防抖合并模板，不调 LLM。"
        audit.write(rec)
        return reply

    # 0.5) 闲聊保护：打招呼/致谢/告别/应答/表情包 → 用人设话术直接回，不调 LLM
    # 否则 LLM 会拿历史对话上下文乱发挥（典型如用户说"你好"被回答之前的卡人脸话题）
    chit = _is_chit_chat(raw_text)
    if chit:
        reply = _chit_chat_reply(chit)
        rec.answer = reply
        rec.answer_level = "B"
        rec.raw_llm_answer = f"[闲聊保护:{chit}] 无具体意图，用人设话术，不调 LLM"
        audit.write(rec)
        return reply

    # 取最近 10 轮历史
    history = memory.recent(sender, rounds=10)

    # 1) 红线词 → 立即转人工 + 紧急告警
    if filters.hit_redline(raw_text):
        rec.redline_hit = True
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason="命中红线词", level="urgent")

    # 1.5) 投诉关键词 → 立即转人工 + 紧急告警
    complaint = _hit_complaint(raw_text)
    if complaint:
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason=f"用户表达投诉/不满（命中『{complaint}』）", level="urgent")

    # 1.51) 用户在找人工/求联系方式 → 直接转人工（知识库答不了"怎么联系"，人来对接）
    contact = _hit_contact(raw_text)
    if contact:
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason=f"用户请求联系人工（命中『{contact}』）", level="warn")

    # 1.52) 充值引导 → 直接发对应场景的钉钉文档链接（不走 LLM，链接 100% 原样）
    #        多规则：不同触发词 → 不同链接 + 各自专属话术。从上到下第一条命中胜出。
    #        充值/账号是钱相关敏感场景：发链接给用户的同时，往告警群发一条提醒让人工跟进。
    rc = _match_recharge_rule(raw_text)
    if rc:
        hit_kw, link, reply_tpl = rc
        reply = reply_tpl.replace("{link}", link)
        rec.answer = reply
        rec.answer_level = "B"
        rec.raw_llm_answer = f"[充值引导] 命中『{hit_kw}』→ 发链接 {link}，不调 LLM，已同步告警群"
        conv_id = audit.write(rec)
        # 同步告警（不影响给用户发链接；告警失败也不阻断）
        try:
            first_question, recent_dialog = _format_recent_dialog(sender, raw_text)
            alert.send_alert(
                level="warn",
                title=f"用户咨询充值/账号（命中『{hit_kw}』）",
                user_text=first_question,
                sender_name=sender_name,
                sender_id=sender,
                bot_reply=reply,
                reason=f"用户咨询充值/账号事宜（命中『{hit_kw}』），已自动发文档链接，请人工跟进确认",
                handler_hint="",
                conversation_id=conv_id,
                recent_dialog=recent_dialog,
                issue_summary="",
                at_all=True,
            )
        except Exception as e:
            log.exception("recharge alert dispatch failed: %s", e)
        return reply

    # 1.55) 业务 ID 模式（如 vid-xxxx）→ 用户问的是具体业务记录，标准 QA 答不上来，直接转人工
    #        若配了 vid_link，额外给用户发一个填写链接（让用户补充详情，方便人工处理）
    hit_id = _hit_id_pattern(raw_text)
    if hit_id:
        vid_link = (getattr(settings, "vid_link", "") or "").strip()
        extra = ""
        if vid_link:
            vid_tpl = getattr(settings, "vid_reply", "") or "把这个 ID 的情况填一下 👉 {link}"
            extra = vid_tpl.replace("{link}", vid_link)
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason=f"用户提及业务 ID『{hit_id}』，需人工核查", level="urgent",
                            extra_reply=extra)

    # 1.6) 用户挫败感 / 纠错 → 立即告警（用户说"不对/不是这个/我说我..."等）
    frustration = _hit_frustration(raw_text)
    if frustration:
        # 看看用户最近是不是已经说过类似的（再次纠错说明真不满了）
        recent_frustration = _count_recent_frustration(sender)
        if recent_frustration >= 1:
            return _do_escalate(rec, sender, sender_name, raw_text,
                                reason=f"用户多次表达不满意（命中『{frustration}』，近 5 分钟第 {recent_frustration + 1} 次）",
                                level="urgent")
        # 首次纠错：判断用户是不是"无上下文短句"（如"不对"、"不是"、"错的"、"重新答"）
        # 这种情况下没法让 LLM 兜底，因为 LLM 会编造答案；要主动承认 + 让用户补充
        # 阈值 ≤ 8 字 ≈ 大概率是纯短反馈，不是新问题
        cleaned_len = len(raw_text.replace(" ", "").replace("　", ""))
        if cleaned_len <= 8:
            log.info("frustration short-feedback (no context): sender=%s text=%r", sender, raw_text)
            short_reply = "嗯，看上去我刚才没答到点上。能稍微说详细点吗？比如具体卡在哪一步、什么场景下出现的，我再帮你看看～"
            rec.answer = short_reply
            rec.answer_level = "B"
            rec.raw_llm_answer = f"[挫败短反馈兜底] 命中『{frustration}』，原文长度 {cleaned_len}，不调 LLM 防止编造。"
            audit.write(rec)
            return short_reply
        # 较长的纠错（带新问题描述）继续往下走 KB / LLM
        log.info("frustration first detected: sender=%s word=%s", sender, frustration)

    # 1.7) 同主题连续追问 N 次（按词重叠判定）
    unresolved = _count_recent_unresolved(sender, raw_text)
    # 下限取 2：unresolved 起算就把"当前这条"计为 1，若阈值下限为 1 则每条消息都会
    # 满足 >=1 而无条件转人工。至少要有 1 条历史同主题追问（即 unresolved>=2）才升级。
    repeat_threshold = max(2, int(settings.repeat_threshold or 3))
    if unresolved >= repeat_threshold:
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason=f"连续 {unresolved} 次未解决", level="warn")

    # 1.8) 兜底：最近 2 分钟内已经有 ≥ 3 条 bot 没答好（B/C 级）→ 真的没解决
    # 注意：只数 B/C 级，A 级（命中 KB 给出标准答案）不算没解决，
    # 避免用户熟悉后快速连问多个不同问题被误判为转人工。
    #
    # 阈值从 ≥2 调整为 ≥3 的原因：当前条还未判定级别，原 "burst_unsolved+1" 把当前条
    # 当成第 3 条计入，可能误升级"命中 KB" 的正常问题。改为只看历史已确认的 B/C 级，
    # ≥3 才升级，避免误伤。
    burst_unsolved = _count_recent_unsolved_messages(sender, window_minutes=2)
    if burst_unsolved >= 3:
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason=f"用户 2 分钟内有 {burst_unsolved} 条问题机器人未能解答", level="warn")

    # 2) 知识库匹配（两阶段：dice 粗筛 → LLM 裁判精选）
    matched_item = None
    matched_score = 0.0
    judge_used = False
    judge_reason = ""

    # 2.0) 业务域路由（可选，默认关闭）：先把用户问题分到业务域，缩小粗筛候选池、降误命中。
    # 零回归保护：返回空 set → matcher 退化为全量匹配（与未开启完全一致）。
    # 路由结果只在 domain_router 内部 log.info 记录，不写 rec 字段（避免污染审计字段）。
    domain_filter = domain_router.classify_question(raw_text)

    if getattr(settings, "judge_enabled", True):
        # 取 top-K 候选（dice 粗筛宽门槛，让裁判看到更多候选）
        prefilter = float(getattr(settings, "judge_prefilter_threshold", 0.10) or 0.10)
        topk = matcher.top_k_candidates(
            raw_text,
            k=int(settings.judge_top_k or 5),
            prefilter_threshold=prefilter,
            domain_filter=domain_filter,
        )
        if topk:
            top_item, top_score = topk[0]
            strong_threshold = float(getattr(settings, "judge_strong_threshold", 0.85) or 0.85)
            if top_score >= strong_threshold:
                # 强匹配跳过裁判，直接命中（省 LLM 调用、省延迟）
                matched_item, matched_score = top_item, top_score
            elif top_score >= prefilter:
                # 弱-中匹配走 LLM 裁判
                jr = llm.judge_match(raw_text, topk)
                judge_used = True
                judge_reason = jr.reason
                # 记录裁判的 LLM 开销
                rec.llm_latency_ms = jr.latency_ms
                rec.llm_prompt_tokens = jr.prompt_tokens
                rec.llm_completion_tokens = jr.completion_tokens
                rec.llm_total_tokens = jr.total_tokens
                if jr.success and jr.choice_index >= 1:
                    matched_item, matched_score = topk[jr.choice_index - 1]
                # jr.choice_index == 0 表示 NONE，落 LLM 兜底
                if not jr.success and jr.error:
                    log.warning("judge failed: %s, fallback to dice best_match", jr.error)
                    # 裁判失败兜底：用 dice 原 best_match 逻辑
                    bm = matcher.best_match(raw_text, domain_filter=domain_filter)
                    if bm:
                        matched_item, matched_score = bm
    else:
        # 关闭裁判 → 用原 dice best_match
        bm = matcher.best_match(raw_text, domain_filter=domain_filter)
        if bm:
            matched_item, matched_score = bm

    if matched_item is not None:
        # 低分裁判防线：即使 LLM 裁判"选中"了候选，原始 dice 分数过低（<0.30）
        # 说明字面相似度极弱，裁判很可能是被 reason 文字误导，不能信。
        # 这种情况按"未匹配"处理：产品问题 → 转人工，避免乱答。
        low_confidence_threshold = 0.30
        if judge_used and matched_score < low_confidence_threshold:
            log.info("low-confidence KB hit reject: q=%r score=%.2f kb_id=%s",
                     raw_text, matched_score, matched_item.id)
            rec.raw_llm_answer = (
                f"[低分裁判拒绝] kb_id={matched_item.id} score={matched_score:.2f} "
                f"reason={judge_reason}（低于阈值 {low_confidence_threshold}，不可信）"
            )
            if _is_product_question(raw_text):
                return _do_escalate(rec, sender, sender_name, raw_text,
                                    reason=f"KB 匹配可信度低（dice={matched_score:.2f}），转人工核实",
                                    level="warn")
            # 非产品问题（闲聊类）继续走 LLM 兜底
            matched_item = None

    if matched_item is not None:
        item = matched_item
        if settings.rephrase_kb_hit:
            r = llm.rephrase(raw_text, item.answer, history=history)
            final_answer = r.text
            # 注意：rephrase 的 token 会覆盖上面 judge 的 token；
            # 真实总开销 = judge token + rephrase token，这里只记录后者作为主要审计。
            rec.llm_url = r.url
            rec.llm_status = r.status
            rec.llm_latency_ms = (rec.llm_latency_ms or 0) + r.latency_ms
            rec.llm_prompt_tokens = (rec.llm_prompt_tokens or 0) + r.prompt_tokens
            rec.llm_completion_tokens = (rec.llm_completion_tokens or 0) + r.completion_tokens
            rec.llm_total_tokens = (rec.llm_total_tokens or 0) + r.total_tokens
            if not r.success and r.error:
                rec.error_msg = f"rephrase: {r.error[:200]}"
        else:
            final_answer = item.answer
        rec.answer = final_answer
        rec.answer_level = "A"
        judge_tag = f" [裁判选中 reason={judge_reason}]" if judge_used else ""
        rec.raw_llm_answer = f"[KB 命中 id={item.id} score={matched_score:.2f}]{judge_tag} 标准答案: {item.answer}"
        audit.write(rec)
        return final_answer

    # 3) KB 未命中：统一走 LLM 兜底
    #    强业务/合规域（_is_product_question）的判定保留供下方对 LLM 结果做二次保险用，
    #    不再直接转人工 —— 之前太宽容易把"侵权怎么解决"这种问题全部挡掉，
    #    现在先让 LLM 试着答，答不出再转人工。
    # 3) 脱敏 → LLM 兜底（仅用于非产品域问题）
    desens, placeholders = filters.mask(raw_text)
    rec.question_desensitized = desens

    # RAG：检索与问题最相关的少量 KB 片段喂给 LLM，替代全量 678 条 KB（大幅省 token）。
    # 复用 matcher dice 粗筛（零新依赖）。检索为空 → 传空串，ask 用骨架 prompt（人设+格式）。
    rag_k = int(getattr(settings, "llm_rag_top_k", 8) or 8)
    rag_candidates = matcher.top_k_candidates(
        raw_text, k=rag_k, prefilter_threshold=0.05, domain_filter=domain_filter)
    kb_context = qa_store.format_kb_snippets(rag_candidates)

    r = llm.ask(desens, history=history, kb_context=kb_context)
    rec.raw_llm_answer = r.text
    rec.llm_url = r.url
    rec.llm_status = r.status
    # 用 += 累加：低分裁判拒绝后落到兜底时，裁判调用的 token 已记在 rec 上（见上文
    # judge 分支），这里若用 = 覆盖会丢掉裁判那次调用的开销，导致 token 统计偏低。
    rec.llm_latency_ms = (rec.llm_latency_ms or 0) + r.latency_ms
    rec.llm_prompt_tokens = (rec.llm_prompt_tokens or 0) + r.prompt_tokens
    rec.llm_completion_tokens = (rec.llm_completion_tokens or 0) + r.completion_tokens
    rec.llm_total_tokens = (rec.llm_total_tokens or 0) + r.total_tokens

    if not r.success:
        # LLM 调用失败也走转人工告警链路
        rec.error_msg = r.error
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason=f"模型调用失败：{(r.error or '')[:60]}",
                            level="warn")

    # 3.5) LLM 主动投降：答不上来时先给一句简单方向/坦诚说明，再输出 <<ESCALATE>> 标记
    #      （见 llm.ask 的转人工规则）→ 保留标记前那句话当前缀，后面接转人工话术
    if "<<ESCALATE>>" in (r.text or ""):
        lead_in = (r.text or "").split("<<ESCALATE>>", 1)[0].strip()
        rec.raw_llm_answer = f"[LLM 投降转人工] 前置语:{lead_in[:60]!r} 原始:{r.text[:120]}"
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason="知识库无对应答案，机器人答不上来，转人工",
                            level="warn", prefix_reply=lead_in)

    # 3.6) 幻觉检测：LLM 兜底回答里若提到知识库里查无此物的"模型名/产品名"
    #      （如把不存在的 seedream 当图片模型推荐），说明 LLM 在编造事实 → 转人工，
    #      不把可能误导用户的编造答案发出去。
    halluc = hallucheck.detect(r.text or "", hallucheck.get_kb_corpus_lower())
    if halluc:
        log.warning("hallucination reject: q=%r suspects=%s answer=%r",
                    raw_text, halluc, (r.text or "")[:120])
        rec.raw_llm_answer = f"[幻觉拦截] 疑似编造实体:{halluc} 原始:{(r.text or '')[:120]}"
        return _do_escalate(rec, sender, sender_name, raw_text,
                            reason=f"LLM 兜底回答疑似编造不存在的实体（{','.join(halluc)}），转人工核实",
                            level="warn")

    # 4) 敏感词
    sensitive_hit = filters.hit_sensitive(r.text)
    cleaned = filters.mask_sensitive(r.text)
    rec.sensitive_hit = sensitive_hit

    # 5) 占位符回填
    restored = filters.restore(cleaned, placeholders)

    final = restored + settings.watermark
    rec.answer = final
    rec.answer_level = "B"
    audit.write(rec)
    return final
