"""钉钉媒体上传：把本地图片上传到钉钉，拿到 mediaId。
群消息用 sampleImageMsg + mediaId 发送时，钉钉自己从 CDN 拉，
完全不需要公网 URL，安全可控。

接口：https://oapi.dingtalk.com/media/upload?access_token={token}&type=image
- 图片大小 ≤ 20MB
- 支持格式：jpg, gif, png, bmp
- 返回 media_id（如 @lADxxxx），有效期 3 天
"""
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from ..config import settings

log = logging.getLogger(__name__)

_TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"
# 新版 OpenAPI：用 downloadCode 换机器人收到消息的文件下载链接
_FILE_DOWNLOAD_URL = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"


def _get_access_token_v2() -> str:
    """获取新版 OpenAPI 的 access_token（放 header x-acs-dingtalk-access-token）。
    新版接口和老版 gettoken 拿到的是同一个 token，直接复用。"""
    return _get_access_token()


def download_user_image(download_code: str) -> dict:
    """把用户私聊/群里发来的图片 downloadCode 换成临时下载 URL，并下载图片字节。

    流程（见 https://open.dingtalk.com/document/isvapp/download-the-file-content-of-the-robot-receiving-message）：
      downloadCode + robotCode → robot/messageFiles/download → downloadUrl → GET 拿字节

    robotCode 优先用 dingtalk_robot_code，留空回退 dingtalk_client_id。
    返回 {ok, content(bytes), download_url, error}。
    """
    if not download_code:
        return {"ok": False, "error": "download_code 为空"}
    robot_code = (settings.dingtalk_robot_code or settings.dingtalk_client_id or "").strip()
    if not robot_code:
        return {"ok": False, "error": "robotCode 未配置（dingtalk_robot_code/client_id 都为空）"}
    try:
        token = _get_access_token_v2()
    except Exception as e:
        return {"ok": False, "error": f"access_token 失败: {e}"}

    try:
        r = httpx.post(
            _FILE_DOWNLOAD_URL,
            headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
            json={"robotCode": robot_code, "downloadCode": download_code},
            timeout=15,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        log.exception("download_user_image: 换取下载URL失败")
        return {"ok": False, "error": f"换取下载URL失败: {e}"}

    download_url = (data or {}).get("downloadUrl") or ""
    if r.status_code != 200 or not download_url:
        log.warning("download_user_image failed: status=%s body=%s", r.status_code, r.text[:300])
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

    # 下载图片字节
    try:
        ir = httpx.get(download_url, timeout=30)
        if ir.status_code != 200:
            return {"ok": False, "error": f"下载图片 HTTP {ir.status_code}", "download_url": download_url}
        return {"ok": True, "content": ir.content, "download_url": download_url}
    except Exception as e:
        log.exception("download_user_image: 下载字节失败")
        return {"ok": False, "error": f"下载字节失败: {e}", "download_url": download_url}

# 复用 broadcaster 的 token 缓存机制：避免每次重新拉
# 加锁防多线程并发刷新（与 broadcaster.py 同样的考虑）
_token_cache = {"token": "", "expires_at": 0.0}
_token_cache_lock = threading.Lock()


def _get_access_token() -> str:
    now = time.time()
    # 快速路径
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    # 慢速路径加锁
    with _token_cache_lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        if not settings.dingtalk_client_id or not settings.dingtalk_client_secret:
            raise RuntimeError("dingtalk_client_id 或 dingtalk_client_secret 未配置")
        params = {
            "appkey": settings.dingtalk_client_id,
            "appsecret": settings.dingtalk_client_secret,
        }
        r = httpx.get(_TOKEN_URL, params=params, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"获取 access_token 失败 HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("errcode") not in (0, None):
            raise RuntimeError(f"获取 access_token 失败: {data}")
        token = data.get("access_token") or ""
        if not token:
            raise RuntimeError(f"access_token 为空：{data}")
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + int(data.get("expires_in", 7200))
        return token


def upload_image(file_path: Path) -> dict:
    """上传图片到钉钉媒体接口。
    返回 {ok, media_id, created_at, type, error}。
    上传成功后 media_id 用于 sampleImageMsg 消息。
    """
    return _upload_media(file_path, "image", max_bytes=20 * 1024 * 1024,
                         mime_default="image/jpeg")


def upload_video(file_path: Path) -> dict:
    """上传视频到钉钉媒体接口。
    钉钉硬限：视频 ≤ 10MB，否则 errcode=40006 或类似拒收。
    超 10MB 直接前置拒绝，避免无用网络请求。
    返回 {ok, media_id, created_at, type, error}。
    """
    return _upload_media(file_path, "video", max_bytes=10 * 1024 * 1024,
                         mime_default="video/mp4")


def _upload_media(file_path: Path, media_type: str, max_bytes: int, mime_default: str) -> dict:
    """通用媒体上传。media_type ∈ {image, voice, video, file}（钉钉接口规定）"""
    if not file_path.exists():
        return {"ok": False, "error": f"文件不存在: {file_path}"}
    size = file_path.stat().st_size
    if size > max_bytes:
        return {"ok": False, "error": f"{media_type} 超过钉钉接口上限 {max_bytes // 1024 // 1024}MB（当前 {size // 1024 // 1024}MB）"}
    try:
        token = _get_access_token()
    except Exception as e:
        return {"ok": False, "error": f"access_token 失败: {e}"}

    try:
        with file_path.open("rb") as fh:
            files = {"media": (file_path.name, fh, mime_default)}
            r = httpx.post(
                f"{_UPLOAD_URL}?access_token={token}&type={media_type}",
                files=files, timeout=120,
            )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        log.exception("upload_%s failed", media_type)
        return {"ok": False, "error": str(e)}

    if r.status_code != 200 or data.get("errcode") not in (0, None):
        log.warning("upload_%s failed: status=%s body=%s", media_type, r.status_code, r.text[:300])
        return {
            "ok": False,
            "error": f"HTTP {r.status_code} errcode={data.get('errcode')} msg={data.get('errmsg')}",
        }

    media_id = data.get("media_id") or ""
    return {
        "ok": True,
        "media_id": media_id,
        "type": data.get("type"),
        "created_at": data.get("created_at"),
    }
