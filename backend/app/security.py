"""JWT 签发/校验 + FastAPI 依赖。"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt

from .config import settings


def issue(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing token")
    try:
        data = jwt.decode(auth[7:], settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    username = data["sub"]
    # 回查 DB：token 无吊销机制，禁用/降权/删除用户后旧 token 仍在 TTL 内有效，
    # 这里以 DB 的 enabled/role 为准（token 里的 role 仅作签发时快照，不再信任）。
    # 延迟导入避免与 db 模块的循环依赖。
    from .db import SessionLocal, SysUser
    db = SessionLocal()
    try:
        u = db.query(SysUser).filter(SysUser.username == username).first()
    finally:
        db.close()
    if u is None or u.enabled != "1":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User disabled or removed")
    return {"username": username, "role": u.role or "viewer"}


def require_role(*roles: str):
    def _dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
        return user
    return _dep
