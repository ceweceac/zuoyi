"""SSRF 防护：校验"用户/管理员可填的 URL"不指向内网/环回/保留地址。

适用场景：群发 webhook、外链等由管理员在 UI 里填写、再由服务端主动请求的 URL。
固定的钉钉/LLM 出向端点不需要走这里（它们不是用户可控的任意地址）。

策略：解析 host → 解析出的所有 IP 必须都是公网可路由地址；
任何一个落在私有/环回/链路本地/保留段就拒绝（防 DNS rebinding 退一步的基础防线）。
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
from typing import Tuple


class SSRFError(Exception):
    """URL 指向内网/非法地址，拒绝请求。"""


def _intranet_allowed() -> bool:
    """逃生开关：部分企业把告警/群发 webhook 指向内网中继（自建飞书/企微代理、
    内网钉钉网关）。默认拦内网（安全优先）；显式设 QABOT_ALLOW_INTRANET_WEBHOOK=1
    才放行私有/环回地址，避免在这类合法部署里把告警/群发静默打挂。
    （link-local 169.254 云元数据仍始终拦截，不受此开关影响。）
    """
    return os.environ.get("QABOT_ALLOW_INTRANET_WEBHOOK") == "1"


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 解析不了的当成不安全
    # link-local（含 169.254 云元数据）、multicast、unspecified 始终拦截，
    # 即便开了内网放行也不放——这些不是合法的 webhook 目标。
    if addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return True
    # 私有/环回/保留段：默认拦，开了逃生开关则放行（内网中继部署）
    if addr.is_private or addr.is_loopback or addr.is_reserved:
        return not _intranet_allowed()
    return False


def check_url(url: str) -> Tuple[bool, str]:
    """返回 (是否放行, 原因)。放行时原因为空串。"""
    u = (url or "").strip()
    if not u:
        return False, "URL 为空"
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("http", "https"):
        return False, f"协议不允许：{parsed.scheme or '(空)'}（仅允许 http/https）"
    host = parsed.hostname
    if not host:
        return False, "URL 缺少主机名"
    # 解析该 host 的所有 IP，逐个校验
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, f"域名解析失败：{e}"
    ips = {info[4][0] for info in infos}
    if not ips:
        return False, "域名未解析到任何 IP"
    for ip in ips:
        if _ip_is_blocked(ip):
            return False, f"目标 {host} 解析到内网/保留地址 {ip}，已拒绝（防 SSRF）"
    return True, ""


def assert_url(url: str) -> None:
    """校验不通过则抛 SSRFError。"""
    ok, reason = check_url(url)
    if not ok:
        raise SSRFError(reason)
