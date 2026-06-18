import logging
import threading
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db, SysUser, verify_password, hash_password
from ..security import issue

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger(__name__)

# ── 登录失败限频（进程内）──────────────────────────────
# 按 (用户名, 客户端IP) 计失败次数；连续失败达阈值后指数退避锁定，
# 防止对默认弱口令（admin/admin123 等）在线爆破。单进程部署足够；
# 多副本场景需换 Redis 等共享存储。
_FAIL_LOCK = threading.Lock()
_FAILURES: dict = {}            # key -> (fail_count, locked_until_ts)
_MAX_FAILS_BEFORE_LOCK = 5      # 连续失败到此值开始锁定
_BASE_LOCK_SECONDS = 30         # 锁定基数，按超出次数指数增长，封顶 1 小时
_MAX_LOCK_SECONDS = 3600
_GC_MAX_ENTRIES = 10000

# 固定的 dummy bcrypt 哈希：用户不存在/被禁用时也跑一次 verify，
# 消除"用户是否存在"的时序差异（用户枚举）。
_DUMMY_HASH = hash_password("a-non-matching-dummy-password")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _check_locked(key: str):
    """返回剩余锁定秒数（>0 表示仍锁定），未锁定返回 0。"""
    now = time.time()
    with _FAIL_LOCK:
        hit = _FAILURES.get(key)
        if not hit:
            return 0
        _, locked_until = hit
        if locked_until > now:
            return int(locked_until - now)
    return 0


def _record_failure(key: str):
    now = time.time()
    with _FAIL_LOCK:
        # 简单 GC：条目过多时清理已过期项
        if len(_FAILURES) > _GC_MAX_ENTRIES:
            for k in [k for k, (_, lu) in _FAILURES.items() if lu <= now]:
                _FAILURES.pop(k, None)
        count = (_FAILURES.get(key, (0, 0))[0]) + 1
        locked_until = 0
        if count >= _MAX_FAILS_BEFORE_LOCK:
            over = count - _MAX_FAILS_BEFORE_LOCK
            lock = min(_BASE_LOCK_SECONDS * (2 ** over), _MAX_LOCK_SECONDS)
            locked_until = now + lock
        _FAILURES[key] = (count, locked_until)


def _reset_failures(key: str):
    with _FAIL_LOCK:
        _FAILURES.pop(key, None)


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    key = f"{body.username}|{_client_ip(request)}"
    remaining = _check_locked(key)
    if remaining > 0:
        return {"ok": False, "msg": f"尝试过于频繁，请 {remaining} 秒后再试"}

    u = db.query(SysUser).filter(SysUser.username == body.username, SysUser.enabled == "1").first()
    # 即使用户不存在/被禁用也跑一次 bcrypt，消除用户枚举的时序差异。
    ok = verify_password(body.password, u.password if u else _DUMMY_HASH)
    if not u or not ok:
        _record_failure(key)
        return {"ok": False, "msg": "用户名或密码错误"}

    _reset_failures(key)
    return {
        "ok": True,
        "token": issue(u.username, u.role),
        "user": {"username": u.username, "displayName": u.display_name, "role": u.role},
    }
