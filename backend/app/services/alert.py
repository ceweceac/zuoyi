"""
转人工告警：把详情推送到钉钉群机器人 webhook。

webhook 安全两种模式：
  1. 关键词模式：webhook 直接调用，消息内必须含特定关键词（推荐"客服报警"之类）
  2. 加签模式：调用时需要 timestamp + sign 参数，会校验密钥
"""
import base64
import hashlib
import hmac
import logging
import time
import urllib.parse

import httpx

from ..config import settings

log = logging.getLogger(__name__)


def _sign(secret: str) -> tuple:
    """钉钉加签：返回 (timestamp_ms, sign)"""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return ts, sign


def send_alert(*,
               level: str,
               title: str,
               user_text: str,
               sender_name: str,
               sender_id: str,
               bot_reply: str,
               reason: str,
               handler_hint: str = "",
               conversation_id: int = 0,
               recent_dialog: str = "",       # 最近几轮的对话上下文（已格式化）
               issue_summary: str = "",       # AI 总结的"用户到底想问什么"
               at_mobiles: list = None,
               at_all: bool = False,
               image_url: str = "",          # 用户发来的图片URL（钉钉可拉则显示）
               image_desc: str = "") -> bool:
    """推送到钉钉群机器人。返回是否成功。"""
    webhook = (settings.alert_webhook or "").strip()
    if not webhook:
        log.warning("alert_webhook 未配置，跳过推送")
        return False

    # SSRF 防线：告警 webhook 由管理员填写，校验不指向内网/保留地址
    # 注意：用 block_reason 接返回，绝不能覆盖入参 reason（它是转人工原因，下方要渲染进卡片）
    from . import netguard
    ok_url, block_reason = netguard.check_url(webhook)
    if not ok_url:
        log.warning("alert_webhook 被安全策略拒绝：%s", block_reason)
        return False

    url = webhook
    secret = (settings.alert_secret or "").strip()
    if secret:
        ts, sign = _sign(secret)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"

    icon = {"urgent": "🚨", "warn": "⚠️", "info": "💬"}.get(level, "💬")
    color = {"urgent": "#d32f2f", "warn": "#f57c00", "info": "#1976d2"}.get(level, "#1976d2")

    user_line = f"**用户**：{sender_name}\n\n" if sender_name else f"**用户**：未知用户  `({sender_id})`\n\n"

    md = (
        f"### {icon} 转人工告警\n\n"
        f"<font color=\"{color}\">**触发原因**：{reason}</font>\n\n"
        f"{user_line}"
        f"---\n\n"
    )

    # 用户的核心问题（本次连续会话中最早问的那一句，真实原话）
    if user_text:
        md += f"#### ❓ 用户原始问题：\n\n> {user_text}\n\n"

    # 用户发来的图片：AI识别结果 + 图片显示
    if image_desc:
        md += f"#### 🖼 图片AI识别：\n\n> {image_desc}\n\n"
    if image_url:
        is_http = image_url.startswith("http://") or image_url.startswith("https://")
        intranet = getattr(settings, "public_base_url_is_intranet", True)
        if is_http and not intranet:
            # 公网可达 → 钉钉能拉取，告警卡片直接嵌图显示
            md += f"![用户图片]({image_url})\n\n"
        elif is_http and intranet:
            # 内网地址 → 钉钉服务器拉不到（嵌图会裂），给同事可点击的链接（内网能开）
            md += f"#### 🖼 用户发来图片\n\n> 钉钉卡片无法直接显示内网图片，[👉 点此查看大图]({image_url})（需在公司内网打开）\n\n"
        else:
            # 相对路径（未配 public_base_url）→ 用 admin_url 兜底拼链接
            admin = (settings.admin_url or "").strip().rstrip("/")
            link = f"{admin}{image_url}" if admin else image_url
            md += f"> ⚠️ 用户发了图片，但未配置访问地址。[点此查看]({link})\n\n"

    # 完整对话历史（仅本次会话的真实条数）
    if recent_dialog:
        md += f"#### 💬 对话历史：\n\n{recent_dialog}\n\n---\n\n"

    if handler_hint:
        md += f"**建议联系人**：{handler_hint}\n\n"

    admin_url = (settings.admin_url or "").strip().rstrip("/")
    if admin_url and conversation_id:
        md += f"\n[👉 查看完整对话历史]({admin_url}/conversations?id={conversation_id})\n\n"
    elif admin_url:
        md += f"\n[👉 打开管理后台]({admin_url}/conversations)\n\n"

    md += f"---\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"

    if at_all:
        md += "\n\n@所有人 请处理"
    elif at_mobiles:
        md += "\n\n" + " ".join(f"@{m}" for m in at_mobiles) + " 请处理"

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": f"转人工：{title}", "text": md},
    }
    at_payload = {}
    if at_all:
        at_payload["isAtAll"] = True
    elif at_mobiles:
        at_payload["atMobiles"] = at_mobiles
    if at_payload:
        payload["at"] = at_payload

    try:
        r = httpx.post(url, json=payload, timeout=10)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("errcode") == 0:
            log.info("alert sent ok, title=%s", title)
            return True
        log.warning("alert send failed: %s %s", r.status_code, r.text[:200])
        return False
    except Exception as e:
        log.exception("alert exception: %s", e)
        return False
