"""
对称加密工具：用于数据库里静态加密 API Key、AppSecret 等敏感字段。

主密钥来源（按优先级）：
1) 环境变量 QABOT_MASTER_KEY（base64 urlsafe 的 32 字节，推荐生产用）
2) 落地文件 data/.master.key（首次启动自动生成，权限 0600）

读取时遇到密文（前缀 ENC::）才解密，明文兼容历史数据。
"""
from __future__ import annotations
import base64
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

_PREFIX = "ENC::"
_KEY_FILE = Path("data/.master.key")
_fernet: Fernet | None = None


class DecryptError(Exception):
    """密文存在但无法解密（master key 变更/密文损坏）。

    显式抛出而非静默返回空串：否则 LLM key / AppSecret 会无声变空，
    机器人停止工作却无任何报错，极难排查。
    """


def _load_or_create_key() -> bytes:
    env = os.environ.get("QABOT_MASTER_KEY")
    if env:
        try:
            # 校验格式
            Fernet(env.encode())
            return env.encode()
        except Exception:
            log.warning("QABOT_MASTER_KEY format invalid, fallback to file key")
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except Exception:
        pass
    log.warning("Generated new master key at %s (chmod 600). 生产环境请改用 QABOT_MASTER_KEY 环境变量。", _KEY_FILE)
    return key


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    if plaintext is None or plaintext == "":
        return ""
    token = _f().encrypt(plaintext.encode("utf-8"))
    return _PREFIX + token.decode("ascii")


def decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        return value  # 明文兼容（迁移期 / 老数据）
    try:
        return _f().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        # 不再静默返回 ""：那会让密钥无声变空、机器人停摆且无报错。
        # 抛出显式异常，由调用方决定记录/告警/跳过。
        log.error("Decrypt failed: master key changed or value corrupted")
        raise DecryptError(
            "密文无法解密：QABOT_MASTER_KEY 可能变更或密文损坏。"
            "请恢复原 master key，或在系统设置里重新填写并保存该密钥。"
        )


def mask(secret: str, head: int = 4, tail: int = 4) -> str:
    """脱敏展示：sk-1234abcd...wxyz"""
    if not secret:
        return ""
    if len(secret) <= head + tail:
        return "*" * len(secret)
    return f"{secret[:head]}{'*' * 8}{secret[-tail:]}"
