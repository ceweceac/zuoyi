"""日志脱敏过滤器：删掉 Bearer token / sk-xxx 这类敏感串。"""
import logging
import re

_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),                  r"\1***"),
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)[^\s\"',]+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{8,}"),                                  "sk-***"),
    (re.compile(r"\bENC::[A-Za-z0-9_\-=]+"),                                "ENC::***"),
]


def _scrub(s: str) -> str:
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


class SecretFilter(logging.Filter):
    """重写 msg/args，先让 logging 内部 % 拼接出原文，再做正则替换。"""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        scrubbed = _scrub(rendered)
        if scrubbed != rendered:
            # 替换为已渲染并脱敏后的字符串，清空 args 避免再次 % 拼接
            record.msg = scrubbed
            record.args = ()
        return True


def install():
    f = SecretFilter()
    # 装到 root logger 上（影响所有 propagate=True 的子 logger）
    root = logging.getLogger()
    root.addFilter(f)
    # 关键：也装到所有 handler 上，因为 uvicorn 的 access logger 可能 propagate=False
    for h in root.handlers:
        h.addFilter(f)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "dingtalk_stream"):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)
