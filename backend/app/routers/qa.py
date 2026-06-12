from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session
import openpyxl
import io
import logging

from ..db import get_db, QaItem
from ..security import current_user, require_role
from ..services.qa_store import store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/qa", tags=["qa"])

# Excel 上传保护
MAX_EXCEL_BYTES = 10 * 1024 * 1024     # 10 MB
MAX_EXCEL_ROWS = 5000                   # 单次最多 5000 行


class QaIn(BaseModel):
    question: str
    answer: str
    category: Optional[str] = None
    tags: Optional[str] = None
    version: Optional[int] = None       # 乐观锁：客户端传上来的当前版本


def _to_dict(it: QaItem) -> dict:
    return {
        "id": it.id, "question": it.question, "answer": it.answer,
        "category": it.category, "tags": it.tags, "status": it.status,
        "version": it.version, "enabled": it.enabled,
        "createdBy": it.created_by, "createdAt": it.created_at.isoformat() if it.created_at else None,
        "updatedBy": it.updated_by, "updatedAt": it.updated_at.isoformat() if it.updated_at else None,
        "approvedBy": it.approved_by, "approvedAt": it.approved_at.isoformat() if it.approved_at else None,
    }


@router.get("")
def list_qa(page: int = 1, size: int = 20,
            keyword: Optional[str] = None, status: Optional[str] = None,
            db: Session = Depends(get_db), _user: dict = Depends(current_user)):
    q = db.query(QaItem).filter(QaItem.deleted == "0")
    if keyword:
        # 转义 LIKE 通配符防 DoS（用户传 % 或 _ 会触发全表慢查询）
        safe = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = q.filter(or_(QaItem.question.like(f"%{safe}%", escape="\\"),
                         QaItem.answer.like(f"%{safe}%", escape="\\")))
    if status:
        q = q.filter(QaItem.status == status)
    total = q.count()
    rows = q.order_by(QaItem.updated_at.desc()).offset((page - 1) * size).limit(size).all()
    return {"records": [_to_dict(r) for r in rows], "total": total}


@router.post("")
def create_qa(body: QaIn, db: Session = Depends(get_db),
              user: dict = Depends(require_role("admin", "editor"))):
    it = QaItem(question=body.question, answer=body.answer, category=body.category,
                tags=body.tags, status="pending", enabled="1", deleted="0", version=1,
                created_by=user["username"], updated_by=user["username"])
    db.add(it); db.commit(); db.refresh(it)
    return _to_dict(it)


@router.put("/{id}")
def update_qa(id: int, body: QaIn, db: Session = Depends(get_db),
              user: dict = Depends(require_role("admin", "editor"))):
    it = db.get(QaItem, id)
    if not it:
        raise HTTPException(404, "not found")
    # 乐观锁：客户端传 version 时必须匹配，否则返回 409（并发修改冲突）
    if body.version is not None and it.version is not None and body.version != it.version:
        raise HTTPException(409, f"版本冲突：服务器版本 {it.version}，你的版本 {body.version}。请刷新后重试。")
    it.question = body.question
    it.answer = body.answer
    it.category = body.category
    it.tags = body.tags
    it.status = "pending"
    it.version = (it.version or 1) + 1
    it.updated_by = user["username"]
    db.commit(); db.refresh(it)
    # 编辑后条目转 pending 且内容已变，但 KB 进程内缓存仍持有旧的 approved 版本，
    # 必须 reload 让缓存按新 store.version 失效重建，否则机器人继续用旧答案应答。
    store.reload()
    return _to_dict(it)


@router.post("/{id}/approve")
def approve(id: int, db: Session = Depends(get_db),
            user: dict = Depends(require_role("admin", "auditor"))):
    it = db.get(QaItem, id)
    if not it:
        raise HTTPException(404, "not found")
    it.status = "approved"
    it.approved_by = user["username"]
    it.approved_at = datetime.utcnow()
    db.commit()
    store.reload()
    return {"ok": True}


@router.post("/{id}/disable")
def disable(id: int, db: Session = Depends(get_db),
            _user: dict = Depends(require_role("admin", "editor"))):
    it = db.get(QaItem, id)
    if not it:
        raise HTTPException(404, "not found")
    it.enabled = "0"
    db.commit()
    store.reload()
    return {"ok": True}


@router.delete("/{id}")
def delete(id: int, db: Session = Depends(get_db),
           _user: dict = Depends(require_role("admin", "editor"))):
    it = db.get(QaItem, id)
    if not it:
        raise HTTPException(404, "not found")
    it.deleted = "1"
    db.commit()
    store.reload()
    return {"ok": True}


@router.post("/reload")
def reload(_user: dict = Depends(require_role("admin", "editor"))):
    store.reload()
    return {"ok": True, "version": store.version}


@router.post("/import")
async def import_excel(file: UploadFile = File(...),
                       db: Session = Depends(get_db),
                       user: dict = Depends(require_role("admin", "editor"))):
    # 大小限制：先看 Content-Length 头（如果有），再读
    if hasattr(file, "size") and file.size and file.size > MAX_EXCEL_BYTES:
        raise HTTPException(413, f"文件太大（{file.size} 字节），最多 {MAX_EXCEL_BYTES} 字节")
    content = await file.read()
    if len(content) > MAX_EXCEL_BYTES:
        raise HTTPException(413, f"文件太大（{len(content)} 字节），最多 {MAX_EXCEL_BYTES} 字节")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(400, f"Excel 解析失败：{e}")
    sh = wb.active

    # 用已有 question 集合做去重（防止重复导入）
    existing_qs = set(q for (q,) in db.query(QaItem.question).filter(QaItem.deleted == "0").all())

    n_added = 0
    n_dup = 0
    n_invalid = 0
    n_too_long = 0
    n_seen_in_batch = set()
    for i, row in enumerate(sh.iter_rows(min_row=2, values_only=True), start=2):
        if i - 1 > MAX_EXCEL_ROWS:
            log.warning("import_excel: 行数超过上限 %d，截断", MAX_EXCEL_ROWS)
            break
        if not row or not row[0] or not row[1]:
            n_invalid += 1
            continue
        q = str(row[0]).strip()
        a = str(row[1]).strip()
        # 字段长度保护（DB 字段是 VARCHAR(1000) / TEXT，超长在 MySQL 上会爆）
        if len(q) > 1000 or len(a) > 10000:
            n_too_long += 1
            continue
        # 去重（DB 已有 + 本次 batch 内）
        if q in existing_qs or q in n_seen_in_batch:
            n_dup += 1
            continue
        n_seen_in_batch.add(q)
        try:
            db.add(QaItem(
                question=q, answer=a,
                category=(str(row[2]).strip() if len(row) > 2 and row[2] else None),
                tags=(str(row[3]).strip() if len(row) > 3 and row[3] else None),
                status="pending", enabled="1", deleted="0", version=1,
                created_by=user["username"], updated_by=user["username"],
            ))
            n_added += 1
        except Exception as e:
            log.warning("import_excel row %d failed: %s", i, e)
            n_invalid += 1
    db.commit()
    return {
        "ok": True,
        "imported": n_added,
        "duplicated": n_dup,
        "invalid": n_invalid,
        "too_long": n_too_long,
    }
