from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, create_engine, Index
from sqlalchemy.orm import declarative_base, sessionmaker
import os
from pathlib import Path

from passlib.context import CryptContext

from .config import settings

# 用基于 __file__ 的绝对路径，避免不同 cwd（如 systemd / supervisor / 容器）启动时目录漂移
# __file__ = backend/app/db.py → 往上 2 层 = backend/
_BACKEND_DIR = Path(__file__).resolve().parent.parent
os.makedirs(str(_BACKEND_DIR / "data"), exist_ok=True)
os.makedirs(str(_BACKEND_DIR / "logs"), exist_ok=True)

# bcrypt 密码哈希
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """密码校验：必须走 bcrypt。

    历史明文兼容已下线（2026/06）—— 之前迁移期允许过明文比较，
    现在所有用户密码都已是 bcrypt 哈希（$2a$/$2b$ 开头）。
    若发现非 bcrypt 格式（如旧明文残留），直接拒绝并日志警告。
    """
    if not hashed:
        return False
    if not hashed.startswith("$2"):
        import logging
        logging.getLogger(__name__).warning(
            "verify_password: 发现非 bcrypt 格式密码（疑似旧明文残留），已拒绝。需要管理员重置该账号密码。"
        )
        return False
    try:
        return pwd_ctx.verify(plain, hashed)
    except Exception:
        return False

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class QaItem(Base):
    __tablename__ = "qa_item"
    __table_args__ = (
        Index("ix_qa_alive", "deleted", "status", "enabled"),
        Index("ix_qa_updated_at", "updated_at"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(String(1000), nullable=False)
    answer = Column(Text, nullable=False)
    category = Column(String(64))
    tags = Column(String(500))
    domains = Column(String(255))                          # 业务域标签（逗号分隔，多标签），供域路由缩小候选池用
    status = Column(String(16), default="pending")        # pending/approved/disabled
    version = Column(Integer, default=1)
    enabled = Column(String(1), default="1")
    deleted = Column(String(1), default="0")
    created_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(64))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    approved_by = Column(String(64))
    approved_at = Column(DateTime)


class Conversation(Base):
    __tablename__ = "conversation"
    __table_args__ = (
        Index("ix_conv_sender_created", "sender", "created_at"),
        Index("ix_conv_created_at", "created_at"),
        Index("ix_conv_level", "answer_level"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    sender = Column(String(64))                    # 钉钉发送人 staffId
    sender_name = Column(String(128))              # 发送人姓名（钉钉回传）
    question = Column(String(2000))                # 原始问题
    question_desensitized = Column(String(2000))   # 脱敏后发给 LLM 的问题
    answer = Column(Text)                          # 最终回复用户的文本
    raw_llm_answer = Column(Text)                  # 模型原始返回（未脱敏/未水印）
    answer_level = Column(String(1))               # A 精确 / B AI / C 转人工
    escalated = Column(String(1))                  # 是否转人工
    redline_hit = Column(String(1))                # 红线词命中
    sensitive_hit = Column(String(1))              # 敏感词命中
    llm_url = Column(String(500))                  # 调用的完整 URL（出错排查）
    llm_model = Column(String(128))                # 模型名
    llm_status = Column(Integer)                   # HTTP 状态码
    llm_latency_ms = Column(Integer)               # 调用耗时
    llm_prompt_tokens = Column(Integer)            # 提示词 token
    llm_completion_tokens = Column(Integer)        # 答案 token
    llm_total_tokens = Column(Integer)             # 总 token
    qa_prompt_version = Column(Integer)            # 当时知识库版本
    error_msg = Column(Text)                       # 异常信息
    created_at = Column(DateTime, default=datetime.utcnow)


class SysUser(Base):
    __tablename__ = "sys_user"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False)
    password = Column(String(128), nullable=False)
    display_name = Column(String(128))
    role = Column(String(32), default="viewer")
    enabled = Column(String(1), default="1")


class SysSetting(Base):
    __tablename__ = "sys_setting"
    k = Column(String(64), primary_key=True)
    v = Column(Text)


# ========== 群发推送相关 4 张表 ==========

class UploadedFile(Base):
    """本地上传的图片/视频文件。生成可访问 URL 供群发引用。"""
    __tablename__ = "uploaded_file"
    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(255), nullable=False)         # 存储到磁盘的文件名（如 1716700000_a1b2c3.mp4）
    original_name = Column(String(255))                    # 用户上传时的原始名（用于展示）
    file_type = Column(String(16))                          # image / video / cover
    mime_type = Column(String(64))
    size_bytes = Column(Integer)
    public_url = Column(String(500))                       # 生成的访问 URL（公网/反代后）
    dingtalk_media_id = Column(String(255))                # 钉钉媒体 ID（仅图片，用于 sampleImageMsg，零公网依赖）
    dingtalk_media_uploaded_at = Column(DateTime)          # 上传到钉钉的时间（钉钉 mediaId 仅 3 天有效，过期需重传）
    uploaded_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    deleted = Column(String(1), default="0")


class DingtalkGroup(Base):
    """机器人加入的群列表。机器人入群时由 bot.py 自动写入。"""
    __tablename__ = "dingtalk_group"
    __table_args__ = (
        Index("ix_group_active", "active"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    open_conversation_id = Column(String(128), unique=True, nullable=False)   # 钉钉群唯一 ID
    conversation_title = Column(String(255))                                   # 群名
    robot_code = Column(String(128))                                           # 机器人 code
    added_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime)
    active = Column(String(1), default="1")                                    # 是否启用推送
    note = Column(String(255))                                                 # 管理员备注
    # 群发 @全体 用：自定义机器人 webhook（加签）。配了就在群发时先发一条 text @全体
    webhook_url = Column(String(500))
    webhook_secret = Column(String(255))


class Broadcast(Base):
    """推送任务。可被一次性 / 重复定时计划引用。"""
    __tablename__ = "broadcast"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)                # 内部标题（管理后台展示）
    content_text = Column(Text)                                # 文案
    image_url = Column(String(500))                            # 图片链接（可空）
    video_title = Column(String(255))                          # 视频标题
    video_cover_url = Column(String(500))                      # 视频封面图链接
    video_link = Column(String(500))                           # 视频跳转链接
    target_group_ids = Column(Text)                            # JSON 数组：[1,2,3]，存 dingtalk_group.id
    status = Column(String(16), default="draft")               # draft / scheduled / sent / failed
    created_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_sent_at = Column(DateTime)
    last_result = Column(Text)                                 # 最近一次发送的结果摘要


class BroadcastSchedule(Base):
    """推送计划（绑定一个 broadcast）。支持一次性、每天、每周、每月、cron。"""
    __tablename__ = "broadcast_schedule"
    __table_args__ = (
        Index("ix_sched_due", "enabled", "next_run_at"),
        Index("ix_sched_broadcast", "broadcast_id"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    broadcast_id = Column(Integer, nullable=False)             # → broadcast.id
    schedule_type = Column(String(16))                         # once / daily / weekly / monthly / cron
    schedule_config = Column(Text)                             # JSON: {"datetime": "...", "time": "09:00", "weekdays": [1,3,5], "cron": "..."}
    next_run_at = Column(DateTime)
    enabled = Column(String(1), default="1")
    created_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    last_run_at = Column(DateTime)
    last_result = Column(Text)


class DirectPush(Base):
    """私信推送历史。每点一次「发送私信」记一条。"""
    __tablename__ = "direct_push"
    __table_args__ = (
        Index("ix_direct_push_created", "created_at"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    content_text = Column(Text)                                # 推送正文
    msg_type = Column(String(16), default="text")              # text / markdown
    target_count = Column(Integer, default=0)                  # 提交的目标数（手机号+staffId 去空后）
    user_count = Column(Integer, default=0)                    # 解析出的有效 userId 数
    sent_count = Column(Integer, default=0)                    # 钉钉受理的人数
    unresolved = Column(Text)                                  # JSON 数组：查不到 userId 的手机号
    invalid = Column(Text)                                     # JSON 数组：钉钉判定无效/受限的 userId
    status = Column(String(16), default="sent")               # pending / running / sent / partial / failed
    result_summary = Column(Text)                              # 结果摘要（含错误信息）
    raw_targets = Column(Text)                                 # JSON 数组：原始目标（手机号/staffId），异步任务和重试从这里读
    failed_userids = Column(Text)                              # JSON 数组：发送失败的 userId（供「重试失败」）
    created_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)


class DirectPushExcluded(Base):
    """私信推送排除名单：被隐藏的用户（离职/测试/无效），不再出现在选人列表。
    只隐藏，不删 conversation 记录，可恢复。
    """
    __tablename__ = "direct_push_excluded"
    id = Column(Integer, primary_key=True, autoincrement=True)
    staff_id = Column(String(64), unique=True, nullable=False)  # 钉钉 staffId
    name = Column(String(128))                                 # 姓名（隐藏时一并记下，便于展示）
    reason = Column(String(255))                               # 隐藏原因（离职/测试等，可空）
    created_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)


class DingtalkUserCache(Base):
    """手机号 → userId 缓存。避免大批量推送时重复调 getbymobile（慢+触发限频）。"""
    __tablename__ = "dingtalk_user_cache"
    id = Column(Integer, primary_key=True, autoincrement=True)
    mobile = Column(String(32), unique=True, nullable=False)
    userid = Column(String(128), nullable=False)
    name = Column(String(128))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DirectContact(Base):
    """可复用的私信联系人名单（如各位老师）。给 staffId 起个名字存起来，
    发送时按名字勾选，不用每次记 ID。
    """
    __tablename__ = "direct_contact"
    id = Column(Integer, primary_key=True, autoincrement=True)
    staff_id = Column(String(64), unique=True, nullable=False)  # 钉钉 staffId
    name = Column(String(128), nullable=False)                 # 显示名（老师姓名）
    tag = Column(String(64))                                   # 分组标签（可空，留作以后分组）
    created_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(engine)
    _migrate_conversation_columns()
    _migrate_uploaded_file_columns()
    _migrate_dingtalk_group_columns()
    _migrate_qa_item_columns()
    _migrate_direct_push_columns()
    _migrate_broadcast_columns()
    _ensure_indexes()
    seed()


def _migrate_direct_push_columns():
    """轻量迁移：给已有 direct_push 表补 raw_targets / failed_userids 列。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "direct_push" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("direct_push")}
    add_columns = {
        "raw_targets":    "TEXT",
        "failed_userids": "TEXT",
    }
    with engine.begin() as conn:
        for col, typ in add_columns.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE direct_push ADD COLUMN {col} {typ}"))


def _migrate_broadcast_columns():
    """轻量迁移：给 broadcast / broadcast_schedule 补齐新增列。

    这两张表此前没有手写迁移，老库在表结构演进后会缺列（create_all 不改老表），
    发送/调度时触发 OperationalError。这里按当前模型补齐可空列。
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    names = set(insp.get_table_names())

    broadcast_cols = {
        "title":           "VARCHAR(255)",
        "content_text":    "TEXT",
        "image_url":       "VARCHAR(500)",
        "video_title":     "VARCHAR(255)",
        "video_cover_url": "VARCHAR(500)",
        "video_link":      "VARCHAR(500)",
        "target_group_ids":"TEXT",
        "status":          "VARCHAR(16)",
        "created_by":      "VARCHAR(64)",
        "last_sent_at":    "DATETIME",
        "last_result":     "TEXT",
    }
    schedule_cols = {
        "broadcast_id":    "INTEGER",
        "schedule_type":   "VARCHAR(16)",
        "schedule_config": "TEXT",
        "next_run_at":     "DATETIME",
        "enabled":         "VARCHAR(1)",
        "created_by":      "VARCHAR(64)",
        "last_run_at":     "DATETIME",
        "last_result":     "TEXT",
    }

    with engine.begin() as conn:
        if "broadcast" in names:
            existing = {c["name"] for c in insp.get_columns("broadcast")}
            for col, typ in broadcast_cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE broadcast ADD COLUMN {col} {typ}"))
        if "broadcast_schedule" in names:
            existing = {c["name"] for c in insp.get_columns("broadcast_schedule")}
            for col, typ in schedule_cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE broadcast_schedule ADD COLUMN {col} {typ}"))


def _migrate_dingtalk_group_columns():
    """轻量迁移：给 dingtalk_group 补 webhook 字段。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "dingtalk_group" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("dingtalk_group")}
    add_columns = {
        "webhook_url":    "VARCHAR(500)",
        "webhook_secret": "VARCHAR(255)",
    }
    with engine.begin() as conn:
        for col, typ in add_columns.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE dingtalk_group ADD COLUMN {col} {typ}"))


def _ensure_indexes():
    """对已存在的表补建索引（create_all 不会改老表）。SQLite 用 IF NOT EXISTS 幂等。"""
    from sqlalchemy import text
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_qa_alive ON qa_item(deleted, status, enabled)",
        "CREATE INDEX IF NOT EXISTS ix_qa_updated_at ON qa_item(updated_at)",
        "CREATE INDEX IF NOT EXISTS ix_conv_sender_created ON conversation(sender, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_conv_created_at ON conversation(created_at)",
        "CREATE INDEX IF NOT EXISTS ix_conv_level ON conversation(answer_level)",
        "CREATE INDEX IF NOT EXISTS ix_group_active ON dingtalk_group(active)",
        "CREATE INDEX IF NOT EXISTS ix_sched_due ON broadcast_schedule(enabled, next_run_at)",
        "CREATE INDEX IF NOT EXISTS ix_sched_broadcast ON broadcast_schedule(broadcast_id)",
        "CREATE INDEX IF NOT EXISTS ix_direct_push_created ON direct_push(created_at)",
        "CREATE INDEX IF NOT EXISTS ix_direct_excluded_staff ON direct_push_excluded(staff_id)",
        "CREATE INDEX IF NOT EXISTS ix_ding_user_cache_mobile ON dingtalk_user_cache(mobile)",
        "CREATE INDEX IF NOT EXISTS ix_direct_contact_staff ON direct_contact(staff_id)",
    ]
    with engine.begin() as conn:
        for sql in statements:
            try:
                conn.execute(text(sql))
            except Exception:
                pass


def _migrate_uploaded_file_columns():
    """轻量迁移：给 uploaded_file 表补齐新增列。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "uploaded_file" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("uploaded_file")}
    add_columns = {
        "dingtalk_media_id":            "VARCHAR(255)",
        "dingtalk_media_uploaded_at":   "DATETIME",
    }
    with engine.begin() as conn:
        for col, typ in add_columns.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE uploaded_file ADD COLUMN {col} {typ}"))


def _migrate_qa_item_columns():
    """轻量迁移：给 qa_item 表补 domains 列（业务域标签，供域路由用）。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "qa_item" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("qa_item")}
    add_columns = {
        "domains": "VARCHAR(255)",
    }
    with engine.begin() as conn:
        for col, typ in add_columns.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE qa_item ADD COLUMN {col} {typ}"))


def _migrate_conversation_columns():
    """轻量迁移：给 conversation 表补齐新增列。SQLite 兼容 ALTER TABLE ADD COLUMN。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "conversation" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("conversation")}
    add_columns = {
        "sender_name":            "VARCHAR(128)",
        "raw_llm_answer":         "TEXT",
        "llm_url":                "VARCHAR(500)",
        "llm_status":             "INTEGER",
        "llm_prompt_tokens":      "INTEGER",
        "llm_completion_tokens":  "INTEGER",
        "llm_total_tokens":       "INTEGER",
        "qa_prompt_version":      "INTEGER",
        "error_msg":              "TEXT",
    }
    with engine.begin() as conn:
        for col, typ in add_columns.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE conversation ADD COLUMN {col} {typ}"))


def seed():
    db = SessionLocal()
    try:
        if db.query(SysUser).count() == 0:
            db.add_all([
                SysUser(username="admin",   password=hash_password("admin123"),   display_name="管理员", role="admin"),
                SysUser(username="auditor", password=hash_password("auditor123"), display_name="审核员", role="auditor"),
                SysUser(username="editor",  password=hash_password("editor123"),  display_name="运营",   role="editor"),
            ])
        if db.query(QaItem).count() == 0:
            db.add_all([
                QaItem(question="公司工作时间是？", answer="周一至周五 9:00-18:00，午休 12:00-13:30。",
                       category="考勤", status="approved", created_by="admin", approved_by="admin"),
                QaItem(question="如何申请年假？", answer="在 OA 系统 -> 我的申请 -> 休假申请 中提交，主管审批后生效。",
                       category="人事", status="approved", created_by="admin", approved_by="admin"),
                QaItem(question="怎么报销差旅费？", answer="登录财务系统，选择\"差旅报销\"，上传发票后由主管审批。",
                       category="财务", status="approved", created_by="admin", approved_by="admin"),
            ])
        db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
