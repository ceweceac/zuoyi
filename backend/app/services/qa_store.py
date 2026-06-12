"""QA 知识库内存缓存 + system prompt 拼装 + 热更新。"""
import threading
import logging
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal, QaItem

log = logging.getLogger(__name__)


# ===== 格式约束块（放 prompt 末尾，强 anchor）。base / 全量 prompt 都用它收尾 =====
_FORMAT_RULES = [
    "【⚠️ 输出前再次确认 — 不要忽略这一条】",
    "1. 用钉钉聊天的口吻回复，**绝对禁止任何 markdown 符号**：# ## ### ** *** --- > 这些一律不要写出来。",
    "2. 不要罗列 1./2./3. 或 一/二/三 这种长清单，即使用户问的是步骤型问题。",
    "   多步骤用「先 xxx，再 xxx，最后 xxx」的自然话术。",
    "3. 篇幅按问题复杂度来：简单问题一两句话讲清；复杂或多步骤问题可以适当展开，把话说明白，但不啰嗦、不灌水、不给无关的附加建议。",
    "4. 不要在结尾说「希望对你有帮助」「通过以上步骤」「~」这种废话。",
    "5. 知识库里没有的具体参数（数字、范围、阈值）一律不要编造。",
]


def build_base_prompt() -> str:
    """拼装 system prompt 的「骨架」：人设 + 业务背景 + 联系人路由 + 格式约束，
    **不含知识库**。用于 RAG 兜底——把全量 KB 换成检索到的少量相关片段，大幅省 token。
    """
    sb = []
    if settings.bot_persona:
        sb.append(settings.bot_persona)
        sb.append("")
    if settings.llm_system_prompt_header:
        sb.append(settings.llm_system_prompt_header)
        sb.append("")
    background = (getattr(settings, "product_background", "") or "").strip()
    if background:
        sb.append("【业务背景与核心原则】")
        sb.append("以下是你必须理解的业务全貌和核心结论。"
                  "回答任何相关问题时，都要先按下面的口径推理，再结合知识库给出合理答案，"
                  "不要机械复述知识库条目而忽略业务逻辑。")
        sb.append(background)
        sb.append("")
    routing = (getattr(settings, "contact_routing", "") or "").strip()
    if routing:
        sb.append("【场景联系人】当你不知道答案时，从下面挑相关的一条告诉用户，比如「这个你钉钉问下张三」；不要全部列出来，按用户问题挑最相关的一条。")
        sb.append(routing)
        sb.append("")
    return "\n".join(sb)


def format_kb_snippets(items) -> str:
    """把若干 QA 拼成知识库片段文本。items 接受 QaItem 列表或 [(QaItem, score), ...]。
    只拼 Q/A，不带 tags 的「也可能问」行——tags 只对 matcher dice 粗筛有用，对 LLM 是噪声。
    """
    sb = []
    for it in items or []:
        # 兼容 (QaItem, score) 元组
        qa = it[0] if isinstance(it, (tuple, list)) else it
        q = (qa.question or "").strip()
        a = (qa.answer or "").strip()
        if not q and not a:
            continue
        sb.append(f"Q: {q}")
        sb.append(f"A: {a}")
        sb.append("")
    return "\n".join(sb).strip()


class QaStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._prompt = ""
        self._version = 0

    def reload(self):
        # ─── 锁外做 DB IO + 字符串拼装（耗时操作不能 hold lock，否则并发请求阻塞）
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
        # 骨架（人设+背景+联系人）+ 全量 KB + 格式约束。
        # 注意：base 末尾已带换行，直接拼 KB 段即可。
        sb = [build_base_prompt()]
        sb.append("=== 知识库开始 ===")
        for it in items:
            sb.append(f"Q: {it.question}")
            sb.append(f"A: {it.answer}")
            if it.tags:
                sb.append(f"   （也可能问：{it.tags}）")
            sb.append("")
        sb.append("=== 知识库结束 ===")
        sb.append("")
        sb.extend(_FORMAT_RULES)
        new_prompt = "\n".join(sb)
        new_count = len(items)

        # ─── 仅锁内做原子替换 + 版本号自增（毫秒级，不阻塞读）
        with self._lock:
            self._prompt = new_prompt
            self._version += 1
            cur_version = self._version
        log.info("QA prompt reloaded, items=%d, version=%d", new_count, cur_version)

    @property
    def prompt(self) -> str:
        return self._prompt

    @property
    def version(self) -> int:
        return self._version


store = QaStore()
