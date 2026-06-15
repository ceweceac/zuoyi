"""用户发来的图片接入处理：下载 → 存盘 → 生成可访问 URL →（可选）AI 视觉识别。

三阶段能力，外部依赖未配时自动降级，配了就生效：
- 阶段1（无依赖）：下载图片存到 data/uploads/，可在后台对话详情查看
- 阶段2（需 public_base_url）：生成钉钉可拉取的公网 URL，告警群直接显示图片
- 阶段3（需视觉模型）：调多模态 LLM 识别图片内容，附在告警里

视觉模型配置（系统设置 / .env，均可留空 → 不启用阶段3）：
  vision_enabled / vision_base_url / vision_api_key / vision_model
"""
import logging
import time
import secrets
from pathlib import Path
from typing import Optional

import httpx

from ..config import settings
from . import dingtalk_media

log = logging.getLogger(__name__)

_UPLOAD_DIR = Path("data/uploads")


def intake_image(download_code: str) -> dict:
    """处理一张用户图片。返回：
    {ok, public_url, local_path, vision_desc, error}
    - public_url: 配了 public_base_url → 公网URL；否则相对 /files/xxx（告警群拉不到）
    - vision_desc: 配了视觉模型 → AI 识别结果；否则空串
    """
    res = dingtalk_media.download_user_image(download_code)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "下载失败")}

    content = res["content"]
    # 落盘（复用 uploader 的命名规则：时间戳_随机.jpg）
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}_{secrets.token_hex(4)}.jpg"
    path = _UPLOAD_DIR / name
    try:
        path.write_bytes(content)
    except Exception as e:
        log.exception("intake_image: 存盘失败")
        return {"ok": False, "error": f"存盘失败: {e}"}

    # 生成可访问 URL
    base = (getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
    public_url = f"{base}/files/{name}" if base else f"/files/{name}"
    reachable = bool(base)  # 钉钉能否拉到

    # 可选：AI 视觉识别
    vision_desc = ""
    if getattr(settings, "vision_enabled", False):
        vision_desc = _recognize(content)

    return {
        "ok": True,
        "public_url": public_url,
        "reachable": reachable,
        "local_path": str(path),
        "vision_desc": vision_desc,
    }


def _recognize(content: bytes) -> str:
    """调多模态视觉模型识别图片内容。失败/未配置返回空串（不阻断主流程）。"""
    base = (getattr(settings, "vision_base_url", "") or "").strip()
    key = (getattr(settings, "vision_api_key", "") or "").strip()
    model = (getattr(settings, "vision_model", "") or "").strip()
    if not (base and key and model):
        log.info("vision_enabled=1 但 vision_base_url/api_key/model 未配齐，跳过识别")
        return ""
    import base64
    b64 = base64.b64encode(content).decode("ascii")
    # OpenAI 兼容的多模态 messages 格式（qwen-vl / gpt-4o / gemini 等通用）
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "用一句话描述这张图片的主要内容（中文，50字内）。如果有文字，提取关键文字。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "max_tokens": 150,
        "temperature": 0,
    }
    try:
        r = httpx.post(base.rstrip("/") + "/chat/completions",
                       json=body, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        if r.status_code != 200:
            log.warning("vision 识别失败 HTTP %s: %s", r.status_code, r.text[:200])
            return ""
        return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        log.warning("vision 识别异常: %s", e)
        return ""
