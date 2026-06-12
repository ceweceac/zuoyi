from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db, Conversation
from ..security import current_user, require_role
from ..services.qa_store import store

router = APIRouter(prefix="/api", tags=["conversation"])


def _to_dict(c: Conversation) -> dict:
    return {
        "id": c.id, "sender": c.sender, "question": c.question,
        "questionDesensitized": c.question_desensitized, "answer": c.answer,
        "answerLevel": c.answer_level, "escalated": c.escalated,
        "redlineHit": c.redline_hit, "sensitiveHit": c.sensitive_hit,
        "llmLatencyMs": c.llm_latency_ms, "llmModel": c.llm_model,
        "createdAt": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/conversations")
def list_conv(page: int = 1, size: int = 20,
              sender: Optional[str] = None, level: Optional[str] = None,
              escalated: Optional[str] = None,
              db: Session = Depends(get_db), _user: dict = Depends(current_user)):
    q = db.query(Conversation)
    if sender:
        q = q.filter(Conversation.sender == sender)
    if level:
        q = q.filter(Conversation.answer_level == level)
    if escalated:
        q = q.filter(Conversation.escalated == escalated)
    total = q.count()
    rows = q.order_by(Conversation.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return {"records": [_to_dict(r) for r in rows], "total": total}


@router.get("/conversations/unmatched")
def unmatched(limit: int = 30, db: Session = Depends(get_db), _user: dict = Depends(current_user)):
    rows = db.query(
        Conversation.question.label("question"),
        func.count().label("cnt"),
        func.max(Conversation.created_at).label("last_seen"),
    ).filter(Conversation.escalated == "1").group_by(Conversation.question)\
     .order_by(func.count().desc()).limit(limit).all()
    return [{"question": r.question, "cnt": r.cnt,
             "last_seen": r.last_seen.isoformat() if r.last_seen else None} for r in rows]


@router.delete("/conversations/unmatched")
def delete_unmatched(question: str = Query(..., min_length=1, max_length=2000),
                     db: Session = Depends(get_db),
                     _user: dict = Depends(require_role("admin", "editor"))):
    """删除所有 question 完全匹配且 escalated=1 的对话记录（清理未命中长尾）。

    安全要求：
    - 仅 admin / editor 角色可调（与 KB 编辑同权限）
    - question 必须精确匹配（不模糊），避免误删大批
    - 返回删除条数让调用方核对
    """
    q = (question or "").strip()
    if not q:
        raise HTTPException(400, "question 不能为空")
    deleted = db.query(Conversation).filter(
        Conversation.escalated == "1",
        Conversation.question == q,
    ).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted": deleted, "question": q}


@router.get("/conversations/stats")
def stats(db: Session = Depends(get_db), _user: dict = Depends(current_user)):
    total = db.query(Conversation).count()
    escalated = db.query(Conversation).filter(Conversation.escalated == "1").count()
    answered = db.query(Conversation).filter(Conversation.answer_level == "B").count()
    return {"total": total, "escalated": escalated, "answered": answered}


@router.get("/health")
def health():
    return {"status": "UP", "qaPromptVersion": store.version}
