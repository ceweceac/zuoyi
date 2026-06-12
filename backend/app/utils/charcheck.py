"""
启动时全量扫描源码、数据库，发现 U+FFFD 损坏字符就立刻报错。
这是字符损坏的最后防线——确保用户永远看不到 �。
"""
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# 这些位置出现 � 是合法的（是检测/拒绝逻辑本身），跳过
_ALLOW_LINES = {
    # 文件相对路径:行号子串  →  允许出现的原因
}
_ALLOW_FILES = {
    "app/utils/charcheck.py",       # 本文件
    "app/services/log_filter.py",   # 日志脱敏正则
    "app/services/runtime_settings.py",  # 拒绝逻辑里有 �
    "app/services/ingest.py",       # 文本归一化正则
    "app/services/matcher.py",      # 同上
    "app/ui/pages.py",              # 健康检查按钮里有 �
}

_BAD = re.compile("�")


def scan_source(root: str = "app") -> list:
    """扫源码目录，返回 [(filepath, lineno, line)]"""
    issues = []
    for r, ds, fs in os.walk(root):
        ds[:] = [d for d in ds if d not in {"__pycache__", ".venv"}]
        for f in fs:
            if not f.endswith(".py"):
                continue
            rel = os.path.join(r, f)
            if rel in _ALLOW_FILES or rel.replace("\\", "/") in _ALLOW_FILES:
                continue
            try:
                with open(rel, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
            except Exception:
                continue
            for i, line in enumerate(text.split("\n"), 1):
                if _BAD.search(line):
                    issues.append((rel, i, line.strip()[:120]))
    return issues


def scan_db() -> list:
    """扫 DB 里所有用户可见字段。"""
    from ..db import SessionLocal, SysSetting, QaItem, Conversation
    issues = []
    db = SessionLocal()
    try:
        for r in db.query(SysSetting).all():
            if r.k in {"llm_api_key", "dingtalk_client_secret"}:
                continue
            v = r.v or ""
            if _BAD.search(v):
                issues.append(("sys_setting", r.k, v[:80]))
        for q in db.query(QaItem).filter(QaItem.deleted == "0").all():
            if _BAD.search((q.question or "") + " " + (q.answer or "")):
                issues.append(("qa_item", str(q.id), (q.question or "")[:60]))
    finally:
        db.close()
    return issues


def assert_clean(strict: bool = True):
    """启动时调一次。strict=True 时发现源码损坏直接抛 RuntimeError 阻止启动。"""
    src_issues = scan_source()
    db_issues = scan_db()
    if src_issues:
        msg = "❌ 检测到源码中含损坏字符（U+FFFD），可能是编辑器/工具链编码事故：\n"
        for p, i, l in src_issues:
            msg += f"   {p}:{i}  {l}\n"
        msg += "请修复后再启动。"
        if strict:
            raise RuntimeError(msg)
        else:
            log.error(msg)
    if db_issues:
        msg = "⚠️ DB 中含损坏字符（已运行可用，但建议用 UI 重新保存修复）：\n"
        for tbl, k, v in db_issues:
            msg += f"   {tbl}[{k}]: {v}\n"
        log.warning(msg)
    if not src_issues and not db_issues:
        log.info("✅ 字符健康检查通过：源码 + DB 全部干净")
