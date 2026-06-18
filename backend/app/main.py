import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from nicegui import ui

from .config import settings
from .db import init_db
from .services.qa_store import store
from .services import runtime_settings
from .services import log_filter
from .services import scheduler as bcast_scheduler
from .utils import charcheck
from .routers import auth, qa, conversation
from . import bot
from .ui import pages  # noqa: F401  注册 NiceGUI 页面
from .ui import broadcast_page  # noqa: F401  群发推送页

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log_filter.install()
log = logging.getLogger(__name__)


def _enforce_jwt_secret():
    """启动前置检查：JWT secret 必须强且非默认。"""
    s = settings.jwt_secret or ""
    weak_defaults = {
        "", "change-me", "secret",
        "change-me-please-this-is-a-demo-secret-key-32bytes!",
        "change-me-to-random-32-bytes-in-production!!!!",
        "please-change-this-to-32-bytes-random-string-in-production!!",
        "please-change-this-secret-at-least-32-bytes!!",
    }
    if s in weak_defaults or len(s) < 32:
        if os.environ.get("QABOT_ALLOW_WEAK_JWT") == "1":
            log.warning("⚠️  JWT_SECRET 弱/缺失，但 QABOT_ALLOW_WEAK_JWT=1 已临时允许（仅供本地开发！）")
            if not s:
                settings.jwt_secret = secrets.token_urlsafe(32)
                log.warning("已为本次启动随机生成 JWT secret，重启后所有 token 失效。")
            return
        raise RuntimeError(
            "JWT_SECRET 未设置或太弱（至少 32 字节）。\n"
            "  生成：python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "  开发期临时允许：export QABOT_ALLOW_WEAK_JWT=1"
        )


def _warn_default_passwords():
    """启动自检：种子账号仍是默认弱口令则高声告警，提醒生产改密。
    默认账号 + 无限频曾是直接的越权入口（爆破已加限频，但弱口令本身仍需改）。"""
    from .db import SessionLocal, SysUser, verify_password
    defaults = {"admin": "admin123", "auditor": "auditor123", "editor": "editor123"}
    db = SessionLocal()
    try:
        weak = []
        for name, pw in defaults.items():
            u = db.query(SysUser).filter(SysUser.username == name).first()
            if u and u.enabled == "1" and verify_password(pw, u.password):
                weak.append(name)
    finally:
        db.close()
    if weak:
        log.warning(
            "⚠️  以下账号仍在使用默认密码：%s 。生产环境务必尽快在「用户管理」里修改，"
            "否则等同公开弱口令。", "、".join(weak),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _enforce_jwt_secret()
    init_db()
    runtime_settings.load_from_db()
    _warn_default_passwords()
    store.reload()
    # 启动自检：源码中含 U+FFFD（损坏字符）直接拒绝启动；DB 中含则告警
    # 开发期可 export QABOT_SKIP_CHARCHECK=1 跳过
    if os.environ.get("QABOT_SKIP_CHARCHECK") != "1":
        charcheck.assert_clean(strict=True)
    # 钉钉 Stream 是无条件出向连接，连不上时会同步重试。无凭证的环境（本地测试 / CI /
    # verify 隔离实例）设 QABOT_DISABLE_BOT=1 可跳过，让 Web/UI 照常起、不被钉钉重试拖住。
    # 生产默认不设此变量，行为完全不变。
    task = None
    if os.environ.get("QABOT_DISABLE_BOT") == "1":
        log.warning("QABOT_DISABLE_BOT=1，跳过钉钉 Stream 连接（仅 Web/UI 模式）")
    else:
        task = bot.start_in_background()
    bcast_scheduler.start()
    log.info("App started")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
        bcast_scheduler.stop()


app = FastAPI(title="QA Bot", lifespan=lifespan)

# 静态文件目录（群发推送用：本地上传的图片/视频通过 /files/{name} 暴露访问 URL）
_UPLOAD_DIR = Path("data/uploads")
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=str(_UPLOAD_DIR)), name="files")

# REST API（保留，给脚本/测试用）
app.include_router(auth.router)
app.include_router(qa.router)
app.include_router(conversation.router)

# 挂载 NiceGUI 到根路径
# 注意：storage_secret 用单独随机串，与 JWT secret 解耦，互不影响安全边界。
# 多副本部署（多进程/多容器）务必通过 QABOT_UI_STORAGE_SECRET 显式注入同一值，
# 否则各副本签名不一致，用户会在副本间被反复踢下线；未注入时回退随机串（单实例可用，重启即失效）。
_NICEGUI_STORAGE_SECRET = os.environ.get("QABOT_UI_STORAGE_SECRET") or secrets.token_urlsafe(32)
if not os.environ.get("QABOT_UI_STORAGE_SECRET"):
    log.warning("QABOT_UI_STORAGE_SECRET 未设置，已随机生成（重启后所有后台会话失效）。"
                "多副本部署必须显式设置同一值，否则会话无法跨副本共享。")

ui.run_with(
    app,
    title="QA 客服机器人 · 管理后台",
    storage_secret=_NICEGUI_STORAGE_SECRET,
    favicon="🤖",
)
