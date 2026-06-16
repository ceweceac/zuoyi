"""每日运营简报（借鉴 Hermes/OpenClaw 的「主动推送」理念）。

机器人原本 100% 被动（用户问才答）。本模块让它每天主动把躺在库里、
没人盯的运营数据送出去——昨日对话量、转人工率、未命中 TOP、待审 QA。
正是 "有用信息在你开口之前就到了"。

推送目标：复用系统设置里的 alert_webhook（后台配的告警群/人）。
开关：daily_brief_enabled（默认关，后台/.env 开启）。
"""
import logging
from datetime import datetime, timedelta

from ..config import settings

log = logging.getLogger(__name__)


def _build_brief() -> str:
    """汇总最近 24h 运营数据，拼成 markdown 简报。"""
    from ..db import SessionLocal, Conversation, QaItem
    from sqlalchemy import func

    now = datetime.utcnow()
    since = now - timedelta(hours=24)
    db = SessionLocal()
    try:
        total = db.query(Conversation).filter(Conversation.created_at >= since).count()
        escalated = db.query(Conversation).filter(
            Conversation.created_at >= since, Conversation.escalated == "1").count()
        # 未命中 TOP5：近24h 转人工或 B 级兜底、按问题聚合频次
        rows = (
            db.query(Conversation.question, func.count().label("c"))
            .filter(Conversation.created_at >= since,
                    Conversation.escalated == "1",
                    func.length(Conversation.question) > 4,
                    # 排除系统标记（图片/语音等非文本消息），只看真实问题
                    ~Conversation.question.like("[NON_TEXT%"))
            .group_by(Conversation.question)
            .order_by(func.count().desc())
            .limit(5).all()
        )
        pending = db.query(QaItem).filter(QaItem.status == "pending", QaItem.deleted == "0").count()
        kb_total = db.query(QaItem).filter(QaItem.deleted == "0").count()
    finally:
        db.close()

    rate = f"{escalated * 100 // total}%" if total else "—"
    # 北京时间日期（库存 UTC，+8）
    today = (now + timedelta(hours=8)).strftime("%m-%d")
    lines = [
        f"### 📊 DramaTV 客服日报 · {today}",
        f"- 昨日对话：**{total}** 条",
        f"- 转人工：**{escalated}** 条（率 {rate}）",
        f"- 知识库：{kb_total} 条" + (f"，**待审 {pending} 条** ⏳" if pending else "（全部已审）"),
    ]
    if rows:
        lines.append("- 未命中 TOP（建议补 KB）：")
        for i, (q, c) in enumerate(rows, 1):
            lines.append(f"  {i}. {(q or '')[:30]}（{c}次）")
    else:
        lines.append("- 未命中：无 ✅")
    lines.append(f"\n> 自动生成于 {(now + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)


def send_daily_brief():
    """生成并推送每日简报。复用 alert 的钉钉发送链路。"""
    if not getattr(settings, "daily_brief_enabled", False):
        log.info("daily_brief_enabled=False，跳过每日简报")
        return
    try:
        md = _build_brief()
    except Exception as e:
        log.exception("生成每日简报失败")
        return
    # 复用 alert 的 webhook 发送（含签名 + SSRF 校验）
    from . import alert
    try:
        alert.send_alert(
            level="info",
            title="DramaTV 客服日报",
            user_text="",
            sender_name="",
            sender_id="",
            bot_reply="",
            reason="每日运营简报（自动推送）",
            recent_dialog=md,        # 把简报放在对话历史位置渲染
            at_all=True,             # 日报 @所有人，确保群成员看到
        )
        log.info("每日简报已推送")
    except Exception as e:
        log.exception("推送每日简报失败: %s", e)
