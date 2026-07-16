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


def _handle_user_image(msg: ChatbotMessage) -> str:
    """用户发来图片：下载+存图+(可选)AI识别，把结果编进 [NON_TEXT:image] 标记。

    标记格式（pipeline 透传、alert 解析）：
      [NON_TEXT:image|url=<公网URL>|desc=<AI识别,可空>] 用户发了图片
    下载失败则退回原始无内容标记，保证流程不中断。
    """
    try:
        codes = msg.get_image_list() or []
    except Exception:
        codes = []
    if not codes:
        return "[NON_TEXT:image] 用户发了图片"
    try:
        from .services import image_intake
        r = image_intake.intake_image(codes[0])   # 取第一张（多图场景先处理首张）
    except Exception as e:
        log.exception("处理用户图片失败: %s", e)
        return "[NON_TEXT:image] 用户发了图片（下载失败）"
    if not r.get("ok"):
        log.warning("用户图片下载失败: %s", r.get("error"))
        return "[NON_TEXT:image] 用户发了图片（下载失败）"
    url = r.get("public_url", "")
    desc = (r.get("vision_desc") or "").replace("|", "/").replace("]", "）")[:120]
    extra = f"|url={url}"
    if desc:
        extra += f"|desc={desc}"
    n = len(codes)
    tail = f"（共{n}张，已处理首张）" if n > 1 else ""
    return f"[NON_TEXT:image{extra}] 用户发了图片{tail}"


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
                # 图片下载是同步阻塞 IO（下载+识别），必须放线程池，
                # 否则会卡住钉钉消息事件循环，后续消息全部延迟。
                text = await asyncio.to_thread(_handle_user_image, msg)
            elif msgtype == "richText" or msg.rich_text_content is not None:
                # 富文本里可能纯图、纯文字、图文混排
                # 尝试提取里面的文字部分
                try:
                    parts = msg.get_text_list() or []
                    inner_text = "".join(parts).strip()
                except Exception:
                    inner_text = ""
                # 富文本里有没有图？（典型场景：用户截图+配一句文字，如"这是什么情况"）
                try:
                    has_img = bool(msg.get_image_list())
                except Exception:
                    has_img = False
                if has_img:
                    # 图文混排：下载图+识别，把用户文字一起带上，让告警和pipeline都拿到
                    img_marker = await asyncio.to_thread(_handle_user_image, msg)
                    text = (img_marker + " " + inner_text).strip() if inner_text else img_marker
                elif inner_text:
                    text = inner_text   # 纯文字富文本，当普通文本处理
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


async def _stream_supervisor():
    """监督钉钉 Stream 连接，断了自动重连——永不让机器人静默变哑。

    为什么必须自己兜底：SDK 的 async `client.start()` 里 `while True` 循环用
    `async with websockets.connect(...)` 且**外层无 try/except**，网络一抖 `async for`
    抛 ConnectionClosedError，异常直接冒出、整个 start() 任务死掉、永不重连——
    进程还活着、health 还 200，但收不到任何钉钉消息（曾真实发生：连接建立后近 2 天
    无消息，用户发"你好"无回应）。SDK 自带重连的是同步 `start_forever()`，但它用
    asyncio.run() 塞不进已在跑的 FastAPI 事件循环。故在这里做 async 原生的监督重连。
    """
    handler = _Handler()
    _bot_handler_ref["h"] = handler
    backoff = 1
    fails = 0            # 连续失败次数
    alerted = False      # 是否已就本轮故障告过警（避免刷屏）
    ALERT_AFTER = 5      # 连续失败达此次数才告警（约累计 30s+ 仍连不上）
    while True:
        try:
            credential = dingtalk_stream.Credential(settings.dingtalk_client_id,
                                                    settings.dingtalk_client_secret)
            client = dingtalk_stream.DingTalkStreamClient(credential)
            client.register_callback_handler(ChatbotMessage.TOPIC, handler)
            log.info("DingTalk Stream client starting, clientId=%s", settings.dingtalk_client_id)
            await client.start()
            # start() 正常返回也视为断开（SDK 正常不会返回），继续重连。
            # 能连上跑一段说明网络已恢复，重置退避与失败计数，下次断开快速重连。
            log.warning("DingTalk Stream 连接结束，%ds 后重连", backoff)
            backoff = 1
            fails = 0
            alerted = False
        except asyncio.CancelledError:
            log.info("DingTalk Stream 监督任务被取消（正常关停）")
            raise
        except Exception as e:
            fails += 1
            log.exception("DingTalk Stream 连接异常(第%d次)，%ds 后重连: %s", fails, backoff, e)
            # 连续失败到阈值仍恢复不了 → 告警一次（避免"哑了没人知道"）。
            # 告警本身不能拖垮重连循环，放线程池且异常吞掉。
            if fails >= ALERT_AFTER and not alerted:
                alerted = True
                try:
                    from .services import alert
                    await asyncio.to_thread(
                        alert.send_system_alert,
                        "钉钉机器人掉线，自动重连中",
                        f"钉钉 Stream 长连接已连续 {fails} 次重连失败，机器人当前可能收不到消息。"
                        f"系统仍在自动重连；若持续未恢复请检查网络与钉钉凭证。",
                    )
                except Exception as ae:
                    log.warning("掉线告警发送失败: %s", ae)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)   # 指数退避，封顶 30s，避免风暴


def start_in_background() -> asyncio.Task:
    """在 FastAPI 事件循环里挂钉钉 Stream 监督任务（自愈重连）。"""
    return asyncio.create_task(_stream_supervisor())


def get_handler() -> ChatbotHandler:
    """供 broadcaster 调用 reply 接口。bot 启动后才有效。"""
    return _bot_handler_ref.get("h")

