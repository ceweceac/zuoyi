"""本地文件上传与持久化。
图片/视频文件存到 data/uploads/，生成可访问 URL。

URL 形式: {public_base_url}/files/{filename}
public_base_url 在系统设置里配置（如 https://qa-bot.公司.com 或 http://公网IP:端口）
钉钉服务器需要能访问这个 URL，否则群里点开会失败。

安全保障：
- 文件名重命名为 {timestamp}_{random8}.{ext}，避免猜测/遍历
- 类型白名单：仅允许常见图片/视频格式
- 大小限制：图片 ≤ 50MB，视频 ≤ 2GB
"""
import os
import secrets
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Tuple, Optional

from ..db import SessionLocal, UploadedFile
from ..config import settings

log = logging.getLogger(__name__)

# 上传根目录（相对工作目录）
UPLOAD_ROOT = Path("data/uploads")
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}
ALLOWED_ALL = ALLOWED_IMAGE_EXTS | ALLOWED_VIDEO_EXTS

MAX_IMAGE_BYTES = 50 * 1024 * 1024          # 50MB
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024    # 2GB


class UploadError(Exception):
    pass


def _detect_kind(ext: str) -> str:
    if ext in ALLOWED_IMAGE_EXTS:
        return "image"
    if ext in ALLOWED_VIDEO_EXTS:
        return "video"
    return "unknown"


def _safe_filename(original_name: str) -> Tuple[str, str]:
    """生成磁盘存储用的安全文件名，返回 (新文件名, 扩展名)。"""
    ext = ""
    if "." in original_name:
        ext = "." + original_name.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_ALL:
        raise UploadError(f"不支持的文件类型 {ext}，仅允许：{sorted(ALLOWED_ALL)}")
    ts = int(time.time())
    rand = secrets.token_hex(4)
    return f"{ts}_{rand}{ext}", ext


def save_upload(content: bytes, original_name: str, uploaded_by: str) -> dict:
    """保存上传的字节流到本地，并入库。
    对图片：额外同步上传到钉钉媒体接口，拿到 mediaId 存库。
    钉钉 mediaId 用于群发时走 sampleImageMsg，零公网依赖。

    返回 dict: {id, filename, original_name, file_type, size_bytes, public_url,
              dingtalk_media_id, dingtalk_upload_error}
    失败抛 UploadError。
    """
    if not content:
        raise UploadError("文件内容为空")
    size = len(content)
    new_name, ext = _safe_filename(original_name)
    kind = _detect_kind(ext)
    if kind == "image" and size > MAX_IMAGE_BYTES:
        raise UploadError(f"图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB，当前 {size // 1024 // 1024}MB")
    if kind == "video" and size > MAX_VIDEO_BYTES:
        raise UploadError(f"视频不能超过 {MAX_VIDEO_BYTES // 1024 // 1024 // 1024}GB，当前 {size // 1024 // 1024}MB")

    # 落盘
    target = UPLOAD_ROOT / new_name
    target.write_bytes(content)
    try:
        os.chmod(target, 0o644)
    except Exception:
        pass

    # 拼可访问 URL（视频和 mediaId 上传失败时的兜底渠道）
    base = (getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
    if not base:
        # 没配公网 URL 时用相对路径，配置后回填可用
        url = f"/files/{new_name}"
    else:
        url = f"{base}/files/{new_name}"

    # 如果是图片，同步上传到钉钉媒体接口（≤ 20MB 才能上）
    # 如果是视频，钉钉接口要求 ≤ 10MB；超过的话 mediaId 拿不到，群里就只能走 markdown URL
    media_id = None
    media_uploaded_at = None
    dingtalk_error = None
    if kind == "image" and size <= 20 * 1024 * 1024:
        try:
            from . import dingtalk_media
            res = dingtalk_media.upload_image(target)
            if res.get("ok"):
                media_id = res.get("media_id") or None
                media_uploaded_at = datetime.utcnow()
                log.info("dingtalk image uploaded: %s mediaId=%s", new_name, media_id)
            else:
                dingtalk_error = res.get("error", "未知错误")
                log.warning("dingtalk image upload failed for %s: %s", new_name, dingtalk_error)
        except Exception as e:
            dingtalk_error = str(e)
            log.exception("dingtalk image upload exception")
    elif kind == "video" and size <= 10 * 1024 * 1024:
        try:
            from . import dingtalk_media
            res = dingtalk_media.upload_video(target)
            if res.get("ok"):
                media_id = res.get("media_id") or None
                media_uploaded_at = datetime.utcnow()
                log.info("dingtalk video uploaded: %s mediaId=%s", new_name, media_id)
            else:
                dingtalk_error = res.get("error", "未知错误")
                log.warning("dingtalk video upload failed for %s: %s", new_name, dingtalk_error)
        except Exception as e:
            dingtalk_error = str(e)
            log.exception("dingtalk video upload exception")
    elif kind == "video":
        dingtalk_error = f"视频超过 10MB（{size // 1024 // 1024}MB），无法走钉钉 mediaId，群里只能用跳转链接形式"

    db = SessionLocal()
    try:
        rec = UploadedFile(
            filename=new_name,
            original_name=original_name[:255],
            file_type=kind,
            mime_type=_guess_mime(ext),
            size_bytes=size,
            public_url=url,
            dingtalk_media_id=media_id,
            dingtalk_media_uploaded_at=media_uploaded_at,
            uploaded_by=uploaded_by,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return {
            "id": rec.id,
            "filename": rec.filename,
            "original_name": rec.original_name,
            "file_type": rec.file_type,
            "size_bytes": rec.size_bytes,
            "public_url": rec.public_url,
            "dingtalk_media_id": rec.dingtalk_media_id,
            "dingtalk_upload_error": dingtalk_error,
        }
    finally:
        db.close()


def _guess_mime(ext: str) -> str:
    table = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska", ".webm": "video/webm", ".flv": "video/x-flv",
        ".m4v": "video/mp4",
    }
    return table.get(ext, "application/octet-stream")


def delete_file(file_id: int) -> bool:
    """软删 + 物理删除磁盘文件（外部 URL 类型只软删，不动磁盘）。"""
    db = SessionLocal()
    try:
        rec = db.get(UploadedFile, file_id)
        if not rec:
            return False
        # external: 开头的是外部 URL 注册，磁盘上没文件，跳过物理删除
        if not (rec.filename or "").startswith("external:"):
            path = UPLOAD_ROOT / rec.filename
            try:
                if path.exists():
                    path.unlink()
            except Exception as e:
                log.warning("delete file %s failed: %s", path, e)
        rec.deleted = "1"
        db.commit()
        return True
    finally:
        db.close()


def rename_file(file_id: int, new_name: str) -> dict:
    """修改文件的显示名（原始文件名），不动磁盘文件名和钉钉 mediaId。

    返回 {ok, error}
    """
    new_name = (new_name or "").strip()
    if not new_name:
        return {"ok": False, "error": "新名称不能为空"}
    if len(new_name) > 200:
        return {"ok": False, "error": "名称太长（≤ 200 字符）"}
    # 过滤危险字符（避免显示问题）
    bad_chars = set('<>:"|?*\x00')
    if any(c in new_name for c in bad_chars):
        return {"ok": False, "error": "包含不允许的字符：< > : \" | ? *"}

    db = SessionLocal()
    try:
        rec = db.get(UploadedFile, file_id)
        if not rec or rec.deleted == "1":
            return {"ok": False, "error": "文件不存在"}
        rec.original_name = new_name
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def register_external_url(url: str, display_name: str, file_type: str, uploaded_by: str) -> dict:
    """注册一个外部 URL 作为"文件记录"（不下载，仅登记）。
    用于：用户已有现成的公网视频/图片链接（如自家 OSS、企业云盘），
    直接登记到文件库，群发时引用。

    file_type: 'video' 或 'image'
    返回 dict: {ok, id, public_url, error}
    """
    url = (url or "").strip()
    display_name = (display_name or "").strip()
    if not url:
        return {"ok": False, "error": "URL 不能为空"}
    if not (url.lower().startswith("http://") or url.lower().startswith("https://")):
        return {"ok": False, "error": "URL 必须以 http:// 或 https:// 开头"}
    if len(url) > 500:
        return {"ok": False, "error": "URL 太长（≤ 500 字符）"}
    if file_type not in ("video", "image"):
        return {"ok": False, "error": "file_type 必须是 video 或 image"}
    if not display_name:
        # 没填名字用 URL 末尾段兜底
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(url)
            display_name = parsed.path.rsplit("/", 1)[-1] or url
        except Exception:
            display_name = url
    display_name = display_name[:200]

    db = SessionLocal()
    try:
        rec = UploadedFile(
            filename=f"external:{int(time.time())}_{secrets.token_hex(4)}",  # 占位，磁盘没文件
            original_name=display_name,
            file_type=file_type,
            mime_type="external/url",
            size_bytes=0,
            public_url=url,
            uploaded_by=uploaded_by,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return {
            "ok": True,
            "id": rec.id,
            "public_url": rec.public_url,
        }
    finally:
        db.close()


def list_files(file_type: Optional[str] = None) -> list:
    """列出未删除文件。"""
    db = SessionLocal()
    try:
        q = db.query(UploadedFile).filter(UploadedFile.deleted == "0")
        if file_type:
            q = q.filter(UploadedFile.file_type == file_type)
        rows = q.order_by(UploadedFile.id.desc()).all()
        return [{
            "id": r.id,
            "filename": r.filename,
            "original_name": r.original_name,
            "file_type": r.file_type,
            "size_bytes": r.size_bytes,
            "public_url": r.public_url,
            "dingtalk_media_id": r.dingtalk_media_id,
            "uploaded_by": r.uploaded_by,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        } for r in rows]
    finally:
        db.close()
