from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db, SysUser, verify_password
from ..security import issue

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.query(SysUser).filter(SysUser.username == body.username, SysUser.enabled == "1").first()
    if not u or not verify_password(body.password, u.password):
        return {"ok": False, "msg": "用户名或密码错误"}
    return {
        "ok": True,
        "token": issue(u.username, u.role),
        "user": {"username": u.username, "displayName": u.display_name, "role": u.role},
    }
