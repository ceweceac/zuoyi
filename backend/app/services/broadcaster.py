"""群发推送服务（自定义机器人 markdown 模式）。

工作流程：
1. 调用者准备一个 Broadcast 记录（含文案、图片链接、视频三件套、目标群列表）
2. send_broadcast(broadcast_id) → 遍历目标群 → 拼一条 markdown → 用各群自定义机器人 webhook 发送

设计要点：
- **只用自定义机器人 webhook 发一条 markdown**：@全体 + 排版文字 + 图片 全在一个气泡里。
  钉钉只有「自定义机器人 + markdown + isAtAll」能同时做到 @全体 和图文排版。
- markdown 图片**必须是公网可达 URL**（钉钉服务器要去拉）。外链直接用；本地 /files/xxx
  会拼上 public_base_url，没配公网基址则拉不到、显示不出。
- 群必须在「群管理」配置了自定义机器人 webhook，否则无法群发。
"""
import json
import logging
import time
import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime
from typing import List, Tuple

import httpx

from ..config import settings
from ..db import SessionLocal, Broadcast, DingtalkGroup
from . import netguard

log = logging.getLogger(__name__)

# 钉钉自定义机器人 markdown 单条 text 上限约 20000 字节，留余量
_MAX_MARKDOWN_LEN = 18000


def _truncate_markdown(text: str, max_len: int = _MAX_MARKDOWN_LEN) -> str:
    """超长 markdown 智能截断，保留结尾"…(已截断)" 提示。"""
    if not text or len(text) <= max_len:
        return text
    suffix = "\n\n…（内容过长已截断）"
    cut = max_len - len(suffix)
    return text[:cut] + suffix


def _parse_image_urls(image_url: str) -> List[str]:
    """解析 image_url 字段，支持两种格式：
    - 旧格式：单个 URL 字符串，如 '/files/abc.jpg'
    - 新格式：JSON 数组 '["url1","url2",...]'
    返回 URL 列表（去掉空值，最多 9 个）。
    """
    if not image_url:
        return []
    s = image_url.strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return [str(u).strip() for u in arr if str(u).strip()][:9]
        except Exception:
            pass
    return [s][:9]


def _to_public_image_url(url: str) -> str:
    """把图片 URL 规整成钉钉能拉取的公网 URL。
    - http(s):// 外链 → 原样
    - /files/xxx 本地路径 → 拼 public_base_url（没配则原样返回，钉钉拉不到）
    """
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    base = (getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
    if u.startswith("/") and base:
        return base + u
    return u


def _normalize_image_placeholders(content_text: str, n_images: int) -> str:
    """把用户随手写的 图1 / 【图1】 自动规整成标准占位符 [图1]。

    安全约束：只规整「编号 ≤ 实际图片数」的，避免正文里偶然的"图5"被误当占位符。
    - 已经是 [图1] 的不动（负向后顾 (?<!\\[) 跳过）
    - 中文方括号 【图1】 也接受
    - 编号超出图片数量 → 保持原样当普通文字
    """
    import re as _re
    if not content_text or n_images <= 0:
        return content_text

    def _repl(m):
        idx = int(m.group(1))
        return f"[图{idx}]" if 1 <= idx <= n_images else m.group(0)

    # 先处理中文方括号 【图N】 → [图N]
    text = _re.sub(r"【图(\d+)】", _repl, content_text)
    # 再处理裸写的 图N（前面不是 [、后面不是 ]，避免动到已规范的 [图N]）
    text = _re.sub(r"(?<!\[)图(\d+)(?!\])", _repl, text)
    return text


def _parse_interleave(content_text: str, image_urls: list) -> list:
    """把含 [图N] 占位的文案 + 图片列表，拆成有序发送序列。

    返回 [("text", "文字段"), ("img", url), ...] 按出现顺序。
    [图1] 对应 image_urls[0]，[图2] 对应 image_urls[1]，以此类推。
    没有 [图N] 占位则返回 None（表示走原逻辑：文字在前、图片在后）。
    """
    import re as _re
    if not content_text or "[图" not in content_text:
        return None
    if not _re.search(r"\[图\d+\]", content_text):
        return None
    seq = []
    pos = 0
    for m in _re.finditer(r"\[图(\d+)\]", content_text):
        seg = content_text[pos:m.start()].strip()
        if seg:
            seq.append(("text", seg))
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(image_urls):
            seq.append(("img", image_urls[idx]))
        pos = m.end()
    tail = content_text[pos:].strip()
    if tail:
        seq.append(("text", tail))
    return seq or None


def _build_webhook_markdown(b: Broadcast) -> Tuple[str, str, bool]:
    """构建自定义机器人 markdown：排版文字 + 图片(公网URL) + 视频链接。

    返回 (title, md_text, has_unreachable_local_img)。
    - 文案含 [图N] 占位 → 按「文字段→图→文字段→图」交错排版
    - 否则 → 文字在前、图片依次在后
    has_unreachable_local_img=True 表示存在本地图但 public_base_url 没配（会显示不出）。
    """
    imgs = [_to_public_image_url(u) for u in _parse_image_urls(b.image_url or "")]
    has_unreachable = any(
        u and not (u.startswith("http://") or u.startswith("https://")) for u in imgs
    )
    content = _normalize_image_placeholders(b.content_text or "", len(imgs))
    seq = _parse_interleave(content, imgs)

    lines: List[str] = []
    if seq:
        for kind, val in seq:
            if kind == "text":
                lines.append(val)
            else:
                lines.append(f"![image]({val})")
            lines.append("")
    else:
        if content.strip():
            lines.append(content.strip())
            lines.append("")
        for u in imgs:
            if u:
                lines.append(f"![image]({u})")
                lines.append("")

    if b.video_link:
        vt = (b.video_title or "查看视频").strip()
        cover = (b.video_cover_url or "").strip()
        if cover:
            lines.append(f"![cover]({_to_public_image_url(cover)})")
            lines.append("")
        lines.append(f"### 📹 [{vt}]({b.video_link.strip()})")
        lines.append("")

    md = "\n".join(lines).strip() or "(空消息)"
    title = (b.title or "推送")[:60]
    return title, md, has_unreachable


def _webhook_sign(webhook_url: str, secret: str) -> str:
    """钉钉自定义机器人加签：URL 拼 timestamp + sign。secret 为空则原样返回。"""
    if not secret:
        return webhook_url
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={ts}&sign={sign}"


def _send_one_group_markdown(webhook_url: str, secret: str, title: str,
                             md_text: str, at_all: bool = True) -> dict:
    """通过自定义机器人 webhook 发一条 markdown（可 @全体）。
    返回 {ok, http_status, response} 或 {ok:False, error}。
    """
    if not webhook_url:
        return {"ok": False, "error": "no webhook_url"}
    # SSRF 防线：webhook 由管理员填写，校验其不指向内网/环回/保留地址再请求
    ok_url, reason = netguard.check_url(webhook_url)
    if not ok_url:
        log.warning("webhook markdown blocked by SSRF guard: %s", reason)
        return {"ok": False, "error": f"webhook 地址被安全策略拒绝：{reason}"}
    url = _webhook_sign(webhook_url, secret or "")
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title or "推送", "text": _truncate_markdown(md_text)},
        "at": {"isAtAll": bool(at_all)},
    }
    try:
        r = httpx.post(url, json=payload, timeout=20)
        try:
            data = r.json()
        except Exception:
            data = {"status_code": r.status_code, "body": r.text[:300]}
        ok = isinstance(data, dict) and data.get("errcode") == 0
        if not ok:
            log.warning("webhook markdown failed: %s", data)
        return {"ok": ok, "http_status": r.status_code, "response": data}
    except Exception as e:
        log.exception("webhook markdown exception")
        return {"ok": False, "error": str(e)}


def _send_webhook_at_all(webhook_url: str, secret: str, content: str) -> dict:
    """通过自定义机器人 webhook 发一条 text 消息并 @所有人（用于「测试发送」按钮）。"""
    if not webhook_url:
        return {"ok": False, "error": "no webhook_url"}
    ok_url, reason = netguard.check_url(webhook_url)
    if not ok_url:
        log.warning("webhook @all blocked by SSRF guard: %s", reason)
        return {"ok": False, "error": f"webhook 地址被安全策略拒绝：{reason}"}
    url = _webhook_sign(webhook_url, secret or "")
    payload = {
        "msgtype": "text",
        "text": {"content": content},
        "at": {"isAtAll": True},
    }
    try:
        r = httpx.post(url, json=payload, timeout=15)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        ok = data.get("errcode") == 0
        if not ok:
            log.warning("webhook @all failed: %s", data)
        return {"ok": ok, "response": data, "http_status": r.status_code}
    except Exception as e:
        log.exception("webhook @all exception")
        return {"ok": False, "error": str(e)}


def send_broadcast(broadcast_id: int, skip_already_sent: bool = False, at_all: bool = True) -> dict:
    """发送一个 broadcast 到它的所有目标群（自定义机器人单条 markdown）。
    返回汇总: {ok: bool, success: [...], failed: [...], total, warning?}

    skip_already_sent=True 时（重试模式）：从 b.last_result 解析上一次成功的 group_id 列表，
    跳过这些群，只对失败的群重发。
    at_all=True 时每条 markdown 都 @全体（钉钉对 @全体 有每日次数限制）。
    """
    db = SessionLocal()
    try:
        b = db.get(Broadcast, broadcast_id)
        if not b:
            return {"ok": False, "error": f"broadcast #{broadcast_id} 不存在"}

        # 解析目标群
        try:
            target_ids = json.loads(b.target_group_ids or "[]")
        except Exception:
            target_ids = []
        if not target_ids:
            return {"ok": False, "error": "未选择目标群"}

        # 幂等保护：重试模式下跳过上一次成功的群
        already_sent_ids = set()
        if skip_already_sent and b.last_result:
            try:
                prev = json.loads(b.last_result)
                # 优先用全量的 success_ids（不截断）；老记录没有则回退到展示用的 success[:20]
                for gid in prev.get("success_ids") or []:
                    already_sent_ids.add(gid)
                if not already_sent_ids:
                    for s in prev.get("success") or []:
                        if isinstance(s, dict) and s.get("group_id"):
                            already_sent_ids.add(s["group_id"])
            except Exception:
                pass
            if already_sent_ids:
                target_ids = [gid for gid in target_ids if gid not in already_sent_ids]
                log.info("broadcast #%d 重试模式：跳过上次已成功的 %d 个群",
                         broadcast_id, len(already_sent_ids))
                if not target_ids:
                    return {"ok": True, "info": "所有群上次已发成功，无需重试"}

        groups = db.query(DingtalkGroup).filter(
            DingtalkGroup.id.in_(target_ids),
            DingtalkGroup.active == "1",
        ).all()
        if not groups:
            return {"ok": False, "error": "目标群都已禁用或不存在"}

        title, md, has_unreachable = _build_webhook_markdown(b)

        success, failed = [], []
        for g in groups:
            wh = (getattr(g, "webhook_url", "") or "").strip()
            if not wh:
                failed.append({
                    "group_id": g.id, "title": g.conversation_title,
                    "detail": "未配置自定义机器人 webhook，无法群发（请到「群管理」→该群→@全体 里配置）",
                })
                continue
            try:
                from . import crypto
                res = _send_one_group_markdown(
                    wh, crypto.decrypt(getattr(g, "webhook_secret", "") or ""),
                    title, md, at_all=at_all)
                if res.get("ok"):
                    success.append({"group_id": g.id, "title": g.conversation_title})
                else:
                    failed.append({
                        "group_id": g.id, "title": g.conversation_title,
                        "detail": res.get("response") or res.get("error") or "发送失败",
                    })
            except Exception as e:
                log.exception("send to group %s failed", g.id)
                failed.append({"group_id": g.id, "title": g.conversation_title, "detail": str(e)})
            # 防钉钉限频
            time.sleep(0.3)

        warning = None
        if has_unreachable:
            warning = "存在本地上传图片但未配置「文件访问基址」(public_base_url)，这些图在群里显示不出，请改用公网外链或配置公网地址。"

        b.last_sent_at = datetime.utcnow()
        # success_ids 是全量成功群 id（仅整数，体积小，绝不截断）——重试幂等只认它，
        # 避免像 success[:20] 那样被截断后，第 21+ 个已成功的群在重试时被重复群发 @全体。
        # 展示用的 success/failed 详情仍各截 20 条控制体积；last_result 列是 TEXT，不再整体截断。
        b.last_result = json.dumps({
            "success_count": len(success),
            "failed_count": len(failed),
            "success_ids": [s["group_id"] for s in success],
            "success": success[:20],
            "failed": failed[:20],
            **({"warning": warning} if warning else {}),
        }, ensure_ascii=False)
        b.status = "sent" if not failed else ("partial" if success else "failed")
        db.commit()
        return {
            "ok": len(failed) == 0,
            "success": success,
            "failed": failed,
            "total": len(groups),
            **({"warning": warning} if warning else {}),
        }
    finally:
        db.close()
