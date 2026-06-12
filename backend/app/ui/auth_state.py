"""共享：app.storage.user 里存登录态。"""
from nicegui import app


def is_logged_in() -> bool:
    return bool(app.storage.user.get("token"))


def user() -> dict:
    return app.storage.user.get("user") or {}


def role() -> str:
    return user().get("role", "")


def can_approve() -> bool:
    return role() in ("admin", "auditor")


def can_edit_qa() -> bool:
    """编辑 / 禁用 / 删除 / 重载 KB 的角色门槛。viewer / auditor 不能改 KB。"""
    return role() in ("admin", "editor")


def login(token: str, user_info: dict) -> None:
    app.storage.user["token"] = token
    app.storage.user["user"] = user_info


def logout() -> None:
    app.storage.user.clear()
