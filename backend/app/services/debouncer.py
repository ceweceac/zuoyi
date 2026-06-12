"""
对话防抖：同一用户连发多条消息时，等 N 秒收齐后合并成一次发给模型。

- 每位用户独立的 buffer + timer
- 每收到一条消息就重置 timer
- timer 到期 → 把这段时间所有消息拼成一段 → 触发处理
- 处理期间又来了新消息 → 等当前这次回完再启新一轮
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# 同一用户连发的"等待凑齐"窗口（秒）
DEBOUNCE_WINDOW = 1.5
# 单批最多合并多少条（防止用户疯狂连发把上下文撑爆）
MAX_BATCH = 8


class _UserBuffer:
    def __init__(self):
        self.messages: List[str] = []
        self.first_at: float = 0.0
        self.timer: Optional[asyncio.Task] = None
        self.processing: bool = False    # 当前是否正在让模型回复
        # 处理中又来的消息，处理完再开一轮。存 (text, msg_obj) 二元组：
        # 第二轮 flush 必须用这些后到消息自己的 msg_obj 回复，
        # 而不是复用上一批的旧 msg_obj（钉钉 sessionWebhook 与具体消息绑定，
        # 复用旧的可能回到过期/错误的会话）。
        self.pending_after: List[tuple] = []


class Debouncer:
    def __init__(self, handler: Callable[[str, str, str], Awaitable[str]],
                 reply: Callable[[str, object], Awaitable[None]]):
        """
        handler: async (sender, merged_text, sender_name) -> reply_text  实际处理函数
        reply:   async (text, original_msg) -> None  发送回复函数
        """
        self._buffers: Dict[str, _UserBuffer] = {}
        self._lock = asyncio.Lock()
        self._handler = handler
        self._reply = reply

    async def enqueue(self, sender: str, sender_name: str, text: str, msg_obj):
        """收到一条消息时调。"""
        async with self._lock:
            buf = self._buffers.get(sender)
            if buf is None:
                buf = _UserBuffer()
                self._buffers[sender] = buf

            # 正在处理 → 排队，等当前轮结束再触发（连同该消息自己的 msg_obj 一起存）
            if buf.processing:
                buf.pending_after.append((text, msg_obj))
                log.info("queued (processing) sender=%s len=%d", sender, len(buf.pending_after))
                return

            # 还没在处理 → 加入待合并 buffer
            buf.messages.append(text)
            if len(buf.messages) == 1:
                buf.first_at = time.time()

            # 重置 timer
            if buf.timer and not buf.timer.done():
                buf.timer.cancel()
                buf.timer = None

            # 超过 MAX_BATCH 立刻处理，否则等窗口
            if len(buf.messages) >= MAX_BATCH:
                asyncio.create_task(self._flush(sender, sender_name, msg_obj))
            else:
                buf.timer = asyncio.create_task(
                    self._wait_and_flush(sender, sender_name, msg_obj)
                )

    async def _wait_and_flush(self, sender: str, sender_name: str, msg_obj):
        try:
            await asyncio.sleep(DEBOUNCE_WINDOW)
        except asyncio.CancelledError:
            return  # 被新消息重置了，下一次 timer 会负责
        await self._flush(sender, sender_name, msg_obj)

    async def _flush(self, sender: str, sender_name: str, msg_obj):
        async with self._lock:
            buf = self._buffers.get(sender)
            if not buf or not buf.messages:
                return
            batch = buf.messages
            buf.messages = []
            buf.timer = None
            buf.processing = True

        merged = self._merge(batch)
        # 注意：不要在这里吞空消息！
        # pipeline.handle 的 _is_empty_intent (行 565) 会用人设话术回复并写 audit，
        # 如果在 debouncer 这里 return 掉，用户会以为机器人死了。
        log.info("flush sender=%s batch=%d merged=%r", sender, len(batch), merged[:60])
        try:
            answer = await self._handler(sender, merged, sender_name)
            await self._reply(answer, msg_obj)
        except Exception as e:
            log.exception("debouncer handler error: %s", e)
            # 不要让用户以为消息被吞了，给个兜底回复
            try:
                await self._reply("系统繁忙，稍后再试一下哈～", msg_obj)
            except Exception:
                pass
        finally:
            async with self._lock:
                buf = self._buffers.get(sender)
                if buf:
                    buf.processing = False
                    # 处理期间又攒了消息 → 开新一轮
                    if buf.pending_after:
                        # 用最后一条后到消息的 msg_obj 回复（最新的 sessionWebhook 最可靠）
                        next_msg_obj = buf.pending_after[-1][1]
                        for t, _m in buf.pending_after:
                            buf.messages.append(t)
                        buf.pending_after = []
                        buf.first_at = time.time()
                        if buf.timer and not buf.timer.done():
                            buf.timer.cancel()
                        buf.timer = asyncio.create_task(
                            self._wait_and_flush(sender, sender_name, next_msg_obj)
                        )
                    else:
                        # 闲置：从字典里清掉，避免长时间运行内存只增不减
                        self._maybe_evict(sender, buf)

    def _maybe_evict(self, sender: str, buf: "_UserBuffer"):
        """buf 完全空闲（无 messages / pending / timer / 不在处理）→ 从字典移除。"""
        if (not buf.messages
                and not buf.pending_after
                and not buf.processing
                and (buf.timer is None or buf.timer.done())):
            self._buffers.pop(sender, None)

    @staticmethod
    def _merge(batch: List[str]) -> str:
        """把连发的多条合并成一段，方便模型理解。"""
        # 过滤空内容
        batch = [m.strip() for m in batch if m and m.strip()]
        if not batch:
            # 全空 → 返回空字符串，让 pipeline 的 _is_empty_intent 接管
            # （不要返回拼好的"连发模板头"，否则模板会被当成非空文本进 LLM）
            return ""
        if len(batch) == 1:
            return batch[0]
        # 多条按时间顺序，标号让模型清楚是连续追问
        numbered = "\n".join(f"{i+1}. {m}" for i, m in enumerate(batch))
        return f"我刚才连发了几条，你一起看：\n{numbered}"
