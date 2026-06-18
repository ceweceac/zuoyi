"""运行时配置：从 sys_setting 表读，覆盖到 config.settings。UI 改完即时生效。

敏感字段（API Key / AppSecret）：DB 存密文（Fernet），加载时解密到内存。
"""
from typing import Dict
import logging
import threading

from ..db import SessionLocal, SysSetting
from ..config import settings
from . import crypto

log = logging.getLogger(__name__)

# settings 是可变全局单例，load_from_db（5 秒节流热重载线程）/ save_to_db（UI 请求线程）
# 会并发改它的多个字段；读侧（pipeline 在线程池里跑）可能读到「base_url 已更新但 api_key 还是旧值」
# 的中间态。用一把锁保护「成组写」与「成组读」，避免跨字段不一致。
settings_lock = threading.RLock()

# 需要保证一致性的「连接组」字段：一次 LLM 调用必须用同一批，不能新旧混搭。
_LLM_CONN_KEYS = ("llm_enabled", "llm_base_url", "llm_api_key", "llm_model",
                  "llm_temperature", "llm_timeout")


def llm_conn_snapshot() -> Dict[str, object]:
    """原子读取 LLM 连接组字段的一致性快照，供 llm.py 调用时用。"""
    with settings_lock:
        return {k: getattr(settings, k, None) for k in _LLM_CONN_KEYS}

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
    "daily_brief_enabled",
    "daily_brief_cron",
    "failure_replay_enabled",
    "failure_replay_cron",
    "prompt_optimize_enabled",
    "public_base_url_is_intranet",
    "vision_enabled",
    "vision_base_url",
    "vision_api_key",
    "vision_model",
]

# 这些字段在 DB 里加密存、UI 上脱敏展示
SECRET_KEYS = {"llm_api_key", "dingtalk_client_secret", "alert_secret", "vision_api_key"}

# 脱敏占位形如 sk-1********wxyz（crypto.mask：中间固定 8 个 *），或短值全 *。
import re as _re
_MASKED_RE = _re.compile(r"^.{0,4}\*{4,}.{0,4}$")


def _looks_masked(v: str) -> bool:
    """判断一个 SECRET 值是否是脱敏占位（UI 回填、管理员未重输）。
    是 → 视为"未修改"，保存时跳过，避免把星号写进库覆盖真实密钥。"""
    return bool(v) and "*" in v and bool(_MASKED_RE.match(v))


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
    """启动时调一次（也被热重载线程每 5 秒调）：把 DB 里的覆盖项应用到 settings。"""
    db = SessionLocal()
    try:
        rows = db.query(SysSetting).all()
    finally:
        db.close()
    # 先在锁外把 DB 值解密/收集好（DB I/O 慢，不占锁），再在锁内成批 setattr，
    # 让读侧（llm_conn_snapshot）拿到的连接组字段始终一致。
    pending = []
    for r in rows:
        if r.k in EDITABLE_KEYS and r.v is not None:
            raw = r.v
            if r.k in SECRET_KEYS:
                try:
                    raw = crypto.decrypt(raw)
                except crypto.DecryptError:
                    log.error(
                        "敏感配置「%s」解密失败（master key 变更或密文损坏），"
                        "已跳过该字段，机器人相关功能可能不可用，请到系统设置重新填写。",
                        r.k,
                    )
                    continue
            pending.append((r.k, raw))
    with settings_lock:
        for k, raw in pending:
            try:
                setattr(settings, k, _coerce(k, raw))
            except Exception:
                pass


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
        applied = []   # 待应用到内存 settings 的 (k, v)，DB 提交成功后在锁内成批 setattr
        for k, v in updates.items():
            if k not in EDITABLE_KEYS:
                continue
            # 哨兵：SECRET 字段若提交的是脱敏占位（sk-1****wxyz），说明管理员没改它，
            # 跳过写入，保留库里原密文；否则会把星号加密入库，解出来当 API key 全部失败。
            if k in SECRET_KEYS and _looks_masked(str(v)):
                continue
            stored = crypto.encrypt(str(v)) if k in SECRET_KEYS else str(v)
            row = db.get(SysSetting, k)
            if row is None:
                db.add(SysSetting(k=k, v=stored))
            else:
                row.v = stored
            applied.append((k, str(v)))
        db.commit()
        with settings_lock:
            for k, v in applied:
                try:
                    setattr(settings, k, _coerce(k, v))
                except Exception:
                    pass
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
