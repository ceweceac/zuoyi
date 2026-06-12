"""双写审计：JSON 行文件 + conversation 表。"""
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path

from ..db import SessionLocal, Conversation

log = logging.getLogger(__name__)

# 审计文件目录：优先用环境变量 QABOT_LOG_DIR；否则用项目 backend/logs（基于 __file__）。
# 不再用 Path("logs") 相对路径——cwd 不在 backend 时会写错地方导致审计丢失。
def _resolve_log_dir() -> Path:
    env = os.environ.get("QABOT_LOG_DIR", "").strip()
    if env:
        return Path(env)
    # __file__ = backend/app/services/audit.py → 往上 3 层 = backend/
    backend_dir = Path(__file__).resolve().parent.parent.parent
    return backend_dir / "logs"


LOG_DIR = _resolve_log_dir()


@dataclass
class AuditRecord:
    sender: str = ""
    sender_name: str = ""
    question: str = ""
    question_desensitized: str = ""
    answer: str = ""
    raw_llm_answer: str = ""
    answer_level: str = ""
    escalated: bool = False
    redline_hit: bool = False
    sensitive_hit: bool = False
    llm_url: str = ""
    llm_model: str = ""
    llm_status: int = 0
    llm_latency_ms: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_total_tokens: int = 0
    qa_prompt_version: int = 0
    error_msg: str = ""


def write(rec: AuditRecord) -> int:
    """返回新写入的 conversation id（出错返回 0）。

    重要保证：本函数**永不抛出异常**。审计失败不应该阻断用户收到回复，
    内部任何 DB / 文件 / 序列化错误都被捕获并记日志。
    """
    conv_id = 0
    # ─── DB 写入（首选）
    try:
        db = SessionLocal()
        try:
            conv = Conversation(
                sender=rec.sender, sender_name=rec.sender_name,
                question=rec.question, question_desensitized=rec.question_desensitized,
                answer=rec.answer, raw_llm_answer=rec.raw_llm_answer,
                answer_level=rec.answer_level,
                escalated="1" if rec.escalated else "0",
                redline_hit="1" if rec.redline_hit else "0",
                sensitive_hit="1" if rec.sensitive_hit else "0",
                llm_url=rec.llm_url, llm_model=rec.llm_model,
                llm_status=rec.llm_status, llm_latency_ms=rec.llm_latency_ms,
                llm_prompt_tokens=rec.llm_prompt_tokens,
                llm_completion_tokens=rec.llm_completion_tokens,
                llm_total_tokens=rec.llm_total_tokens,
                qa_prompt_version=rec.qa_prompt_version,
                error_msg=rec.error_msg or None,
            )
            db.add(conv)
            db.commit()
            conv_id = conv.id
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log.exception("write conversation failed: %s", e)

    # ─── JSON 行文件兜底
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.utcnow().isoformat(), **asdict(rec)}
        with (LOG_DIR / f"audit-{date.today()}.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log.exception("write audit file failed: %s", e)

    return conv_id
