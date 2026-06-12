"""运行时配置：从 sys_setting 表读，覆盖到 config.settings。UI 改完即时生效。

敏感字段（API Key / AppSecret）：DB 存密文（Fernet），加载时解密到内存。
"""
from typing import Dict
import logging

from ..db import SessionLocal, SysSetting
from ..config import settings
from . import crypto

log = logging.getLogger(__name__)

# 在 UI 里允许编辑的字段（key 与 settings 属性名一致）
EDITABLE_KEYS = [
    "llm_enabled",
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "llm_timeout",
    "llm_temperature",
    "llm_system_prompt_header",
    "bot_persona",
    "product_background",
    "rephrase_kb_hit",
    "judge_enabled",
    "judge_strong_threshold",
    "judge_top_k",
    "llm_rag_top_k",
    "judge_prefilter_threshold",
    "domain_router_enabled",
    "public_base_url",
    "dingtalk_client_id",
    "dingtalk_client_secret",
    "dingtalk_robot_code",
    "escalate_reply",
    "watermark",
    "contact_routing",
    "alert_webhook",
    "alert_secret",
    "escalate_user_reply",
    "complaint_keywords",
    "escalate_id_prefixes",
    "repeat_threshold",
    "admin_url",
    "recharge_rules",
    "recharge_reply",
    "vid_link",
    "vid_reply",
]

# 这些字段在 DB 里加密存、UI 上脱敏展示
SECRET_KEYS = {"llm_api_key", "dingtalk_client_secret", "alert_secret"}


def _coerce(key: str, raw: str):
    """字符串还原成 settings 字段原始类型。"""
    default = getattr(settings, key)
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def load_from_db():
    """启动时调一次：把 DB 里的覆盖项应用到 settings。"""
    db = SessionLocal()
    try:
        rows = db.query(SysSetting).all()
        for r in rows:
            if r.k in EDITABLE_KEYS and r.v is not None:
                raw = r.v
                if r.k in SECRET_KEYS:
                    try:
                        raw = crypto.decrypt(raw)
                    except crypto.DecryptError:
                        # 密钥解不开：跳过该字段（保留 config 默认空值），并高声告警。
                        # 不让整个启动崩掉，但管理员能从日志立刻看到根因。
                        log.error(
                            "敏感配置「%s」解密失败（master key 变更或密文损坏），"
                            "已跳过该字段，机器人相关功能可能不可用，请到系统设置重新填写。",
                            r.k,
                        )
                        continue
                try:
                    setattr(settings, r.k, _coerce(r.k, raw))
                except Exception:
                    pass
    finally:
        db.close()


def save_to_db(updates: Dict[str, str]):
    """UI 保存时调：写 DB + 实时覆盖 settings。敏感字段加密入库。

    安全保护：拒绝写入含 U+FFFD（损坏字符）的值，避免污染 DB。
    """
    # 先全部校验，发现损坏字符就整体拒绝（避免半写状态）
    for k, v in updates.items():
        if k not in EDITABLE_KEYS:
            continue
        if isinstance(v, str) and "�" in v:
            raise ValueError(
                f"字段「{k}」包含损坏的字符（U+FFFD '�'），"
                f"通常是粘贴/编码问题导致。请检查输入。位置示例：'{v[max(0, v.find(chr(0xfffd))-10):v.find(chr(0xfffd))+10]}'"
            )

    db = SessionLocal()
    try:
        for k, v in updates.items():
            if k not in EDITABLE_KEYS:
                continue
            stored = crypto.encrypt(str(v)) if k in SECRET_KEYS else str(v)
            row = db.get(SysSetting, k)
            if row is None:
                db.add(SysSetting(k=k, v=stored))
            else:
                row.v = stored
            try:
                setattr(settings, k, _coerce(k, str(v)))
            except Exception:
                pass
        db.commit()
    finally:
        db.close()


def current_masked() -> Dict[str, object]:
    """给 UI 用：敏感字段返回脱敏占位，普通字段原样返回。"""
    out = {}
    for k in EDITABLE_KEYS:
        v = getattr(settings, k, "")
        if k in SECRET_KEYS:
            out[k] = crypto.mask(str(v or ""))
        else:
            out[k] = v
    return out


def current() -> Dict[str, str]:
    """内部用，返回明文（仅服务器内部，绝不外泄）。"""
    return {k: getattr(settings, k, "") for k in EDITABLE_KEYS}
