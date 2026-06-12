"""对话记忆：从 conversation 表取该用户最近 N 轮对话，构造成 LLM messages 历史。"""
from typing import List, Dict
from sqlalchemy import desc, or_

from ..db import SessionLocal, Conversation


# 这些是失败/兜底的回复，喂给模型只会污染上下文，跳过
_BAD_ANSWERS = (
    "系统繁忙",
    "已为您转人工",
    "需要人工进一步确认",
    "我没你清楚",
    "[LLM 未启用]",
)


def recent(sender: str, rounds: int = 5) -> List[Dict[str, str]]:
    """
    返回最近 rounds 轮（user+assistant 各一条算一轮）按时间正序的消息列表。
    跳过失败 / 兜底回复，否则会污染模型上下文。
    """
    if not sender or rounds <= 0:
        return []
    db = SessionLocal()
    try:
        rows = (
            db.query(Conversation)
            .filter(Conversation.sender == sender)
            # NULL escalated 也算"未转人工"（三值逻辑修复：!='1' 对 NULL 不成立会静默漏掉历史）
            .filter(or_(Conversation.escalated != "1", Conversation.escalated.is_(None)))
            .order_by(desc(Conversation.id))
            .limit(rounds * 4)  # 多取一些，跳过失败的还剩够数（原 *3 在失败率高时不够）
            .all()
        )
    finally:
        db.close()

    rows = list(reversed(rows))
    msgs: List[Dict[str, str]] = []
    for r in rows:
        q = (r.question or "").strip()
        a = (r.answer or "").strip()
        if not q or not a:
            continue
        if any(bad in a for bad in _BAD_ANSWERS):
            continue
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": a})

    # 截到目标轮数（最后 N 轮）
    if len(msgs) > rounds * 2:
        msgs = msgs[-rounds * 2:]
    return msgs
