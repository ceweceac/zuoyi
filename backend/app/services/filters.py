"""请求/响应侧的脱敏与安全过滤。"""
import re
import secrets
from typing import Dict, Tuple, List

from ..config import settings

_PHONE     = re.compile(r"(?<![0-9])1[3-9]\d{9}(?![0-9])")
_ID_CARD   = re.compile(r"(?<![0-9Xx])\d{17}[\dXx](?![0-9Xx])")
_BANK_CARD = re.compile(r"(?<![0-9])\d{16,19}(?![0-9])")
_EMAIL     = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _luhn_check(num: str) -> bool:
    """Luhn 校验：合法银行卡号必须通过此算法。
    用来过滤误命中的订单号 / 流水号（它们多半不符合 Luhn）。
    """
    if not num.isdigit():
        return False
    total = 0
    # 从右往左：第 2、4、6... 位（从右数，下标从 1）需要 *2
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask(text: str) -> Tuple[str, Dict[str, str]]:
    """返回 (脱敏后文本, {占位符: 原文}) ；回复用户前用 restore 还原。

    占位符使用随机 nonce 避免与用户原文里的字面 [PHONE_0] 冲突，
    防止 restore 时把用户原文那部分误替换成被脱敏的真号。
    """
    if not text:
        return "", {}
    placeholders: Dict[str, str] = {}
    counter = [0]
    nonce = secrets.token_hex(3)   # 6 字符随机串，全局唯一

    def _sub(tag: str, pattern: re.Pattern, s: str, luhn: bool = False) -> str:
        def _r(m: re.Match) -> str:
            value = m.group(0)
            # 银行卡走 Luhn 校验，不通过的不脱敏（多半是订单号/流水号）
            if luhn and not _luhn_check(value):
                return value
            key = f"[{tag}_{nonce}_{counter[0]}]"
            counter[0] += 1
            placeholders[key] = value
            return key
        return pattern.sub(_r, s)

    s = text
    s = _sub("PHONE",  _PHONE, s)
    s = _sub("IDCARD", _ID_CARD, s)
    s = _sub("BANK",   _BANK_CARD, s, luhn=True)
    s = _sub("EMAIL",  _EMAIL, s)
    return s, placeholders


def restore(text: str, placeholders: Dict[str, str]) -> str:
    if not text or not placeholders:
        return text
    for k, v in placeholders.items():
        text = text.replace(k, v)
    return text


def hit_redline(text: str) -> bool:
    return _contains_any(text, settings.redline_words)


def hit_sensitive(text: str) -> bool:
    return _contains_any(text, settings.sensitive_words)


def mask_sensitive(text: str) -> str:
    if not text:
        return text
    for w in settings.sensitive_words:
        if w:
            text = text.replace(w, "***")
    return text


def _contains_any(text: str, words: List[str]) -> bool:
    if not text:
        return False
    return any(w and w in text for w in words)
