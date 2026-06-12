"""钉钉 Stream 长连接客户端，复用 pipeline 的对话编排。

加入了消息防抖：用户连发多条消息会被合并成一次回复。

路由策略：
- 私聊（conversation_type=='1'）→ 走 QA pipeline 回复用户
- 群聊（conversation_type=='2'）→ 自动入库该群（用于群发推送），
  默认不回复群消息（避免打扰），只有被 @ 时才走 QA。
"""
import asyncio
import logging
from datetime import datetime

import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotMessage, ChatbotHandler

from .config import settings
from .db import SessionLocal, DingtalkGroup
from .services.pipeline import handle
from .services.debouncer import Debouncer

log = logging.getLogger(__name__)


async def _do_handle(sender: str, merged_text: str, sender_name: str) -> str:
    """调 pipeline（同步函数）放线程池跑，避免阻塞事件循环。"""
    return await asyncio.to_thread(handle, sender, merged_text, sender_name)


async def _do_reply(text: str, msg: ChatbotMessage):
    """钉钉 reply_text 是同步的，也放线程池避免阻塞。"""
    handler = _bot_handler_ref["h"]
    await asyncio.to_thread(handler.reply_text, text, msg)


# 全局单例
_debouncer = Debouncer(handler=_do_handle, reply=_do_reply)
_bot_handler_ref = {"h": None}


def _register_group(msg: ChatbotMessage) -> None:
    """群消息触发时，自动把群入库（用于后续群发推送选群）。"""
    conv_id = (msg.conversation_id or "").strip()
    if not conv_id:
        return
    db = SessionLocal()
    try:
        row = db.query(DingtalkGroup).filter(
            DingtalkGroup.open_conversation_id == conv_id
        ).first()
        if row is None:
            row = DingtalkGroup(
                open_conversation_id=conv_id,
                conversation_title=(msg.conversation_title or "")[:255] or "未命名群",
                robot_code=getattr(msg, "robot_code", "") or "",
                last_active_at=datetime.utcnow(),
                active="1",
            )
            db.add(row)
            log.info("new group registered: id=%s title=%s", conv_id, msg.conversation_title)
        else:
            row.last_active_at = datetime.utcnow()
            if msg.conversation_title and row.conversation_title != msg.conversation_title:
                row.conversation_title = msg.conversation_title[:255]
        db.commit()
    except Exception as e:
        log.exception("register group failed: %s", e)
    finally:
        db.close()


class _Handler(ChatbotHandler):
    async def process(self, callback: dingtalk_stream.CallbackMessage):
        msg = ChatbotMessage.from_dict(callback.data)
        conv_type = (msg.conversation_type or "").strip()
        sender = msg.sender_staff_id or msg.sender_id or ""
        sender_name = getattr(msg, "sender_nick", "") or ""
        text = (msg.text.content or "").strip() if msg.text else ""

        # ─── 非文本消息识别：图片 / 富文本 / 其它 ───
        # 钉钉 ChatbotMessage 的真实字段（SDK 2.x）：
        #   message_type ∈ {'text', 'picture', 'richText', ...}
        #   image_content:    message_type='picture' 时有值
        #   rich_text_content: message_type='richText' 时有值（可能含图）
        # bot 没有图片识别能力，直接打标记让 pipeline 转人工
        msgtype = (msg.message_type or "").strip()
        if not text:
            if msgtype == "picture" or msg.image_content is not None:
                text = "[NON_TEXT:image] 用户发了图片"
            elif msgtype == "richText" or msg.rich_text_content is not None:
                # 富文本里可能纯图、纯文字、图文混排
                # 尝试提取里面的文字部分
                try:
                    parts = msg.get_text_list() or []
                    inner_text = "".join(parts).strip()
                except Exception:
                    inner_text = ""
                if inner_text:
                    text = inner_text   # 把里面的文字当成普通文本处理
                else:
                    text = "[NON_TEXT:richText] 用户发了富文本/图片"
            elif msgtype and msgtype != "text":
                # 其它未知非文本类型：audio / video / file 等
                text = f"[NON_TEXT:{msgtype}] 用户发了非文本消息（{msgtype}）"

        # 群聊（conversation_type == '2'）
        if conv_type == "2":
            # 自动入库该群，方便后续群发推送选群
            try:
                await asyncio.to_thread(_register_group, msg)
            except Exception as e:
                log.exception("register group async error: %s", e)
            # 默认不在群里回应消息（避免干扰）；只有用户主动 @ 机器人时才走 QA
            if not msg.is_in_at_list:
                log.debug("group msg (not @ed): conv=%s text=%s", msg.conversation_id, text[:30])
                return AckMessage.STATUS_OK, "OK"
            # 被 @ 了，走 QA 流程。把 sender 改成 "g:{conv_id}:{user_id}" 形式，
            # 让该用户在群里 @ 的对话历史与私聊上下文完全隔离，避免回答串到无关话题
            conv_id = (msg.conversation_id or "").strip()
            if conv_id and sender:
                sender = f"g:{conv_id}:{sender}"
            log.info("group @ bot: conv=%s sender=%s text=%s",
                     msg.conversation_id, sender, text)

        # 私聊 or 群聊被 @ → 走 QA
        log.info("recv from=%s name=%s text=%s conv_type=%s",
                 sender, sender_name, text, conv_type or "1")
        try:
            # 立即 ACK 钉钉；真实回复走防抖队列异步完成
            await _debouncer.enqueue(sender, sender_name, text, msg)
        except Exception as e:
            log.exception("enqueue error: %s", e)
        return AckMessage.STATUS_OK, "OK"


def start_in_background() -> asyncio.Task:
    """在 FastAPI 事件循环里挂一个钉钉 Stream 任务。"""
    credential = dingtalk_stream.Credential(settings.dingtalk_client_id,
                                            settings.dingtalk_client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    handler = _Handler()
    _bot_handler_ref["h"] = handler
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)
    log.info("DingTalk Stream client starting, clientId=%s", settings.dingtalk_client_id)
    return asyncio.create_task(client.start())


def get_handler() -> ChatbotHandler:
    """供 broadcaster 调用 reply 接口。bot 启动后才有效。"""
    return _bot_handler_ref.get("h")

