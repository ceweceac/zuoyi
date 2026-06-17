"""提示词优化：用户发来一段创作类 prompt 时，机器人帮优化成更完整的版本。

设计原则（避免破坏"客服不乱发挥"的定位）：
- 识别从严：必须像 prompt（够长 + 含创作特征词 + 不是疑问句），否则不碰，
  让消息继续走正常 KB/LLM 流程。宁可漏判，不可误把普通问题/剧本当 prompt 改。
- 优化有据：基于 DramaTV 提示词规范（角色写年龄/服装/场景，视频写动作/镜头/氛围）。
- 给对比：返回"你的原版 + 优化建议"，让用户自己判断，而非直接替换。
"""
import logging
import re

from ..config import settings

log = logging.getLogger(__name__)

# 创作类提示词的特征词（命中 ≥1 个才可能是 prompt）
_CREATIVE_HINTS = (
    "画面", "镜头", "角色", "人物", "场景", "风格", "光影", "氛围", "构图",
    "特写", "全景", "近景", "远景", "运镜", "推进", "拉远", "服装", "造型",
    "背景", "色调", "质感", "写实", "动漫", "古风", "赛博", "电影感",
    "穿着", "表情", "姿态", "妆造", "分镜", "提示词", "prompt",
)
# 疑问/求助特征（命中说明是"问问题"不是"发prompt"，不优化）
_QUESTION_HINTS = (
    "吗", "怎么", "如何", "为什么", "为啥", "是不是", "能不能", "可以吗",
    "?", "？", "怎么办", "什么意思", "哪里", "多少", "几个",
)


def looks_like_prompt(text: str) -> bool:
    """从严判断：这段文本是不是一段待优化的创作 prompt。"""
    if not text:
        return False
    s = text.strip()
    # 1) 显式触发词优先放行（用户主动要求优化，不受长度限制）
    if any(t in s for t in ("优化提示词", "优化这个提示词", "帮我优化prompt",
                            "帮我改提示词", "润色提示词", "完善提示词")):
        return True
    # 2) 够长（短文本多半是闲聊/关键词，不是 prompt）
    if len(s) < 15:
        return False
    # 3) 是疑问/求助 → 是问问题，不是发 prompt
    if any(q in s for q in _QUESTION_HINTS):
        return False
    # 4) 含创作特征词 ≥2 个才算（单个词太宽松，易误判）
    hits = sum(1 for w in _CREATIVE_HINTS if w in s)
    return hits >= 2


def optimize(text: str) -> str:
    """调 LLM 把用户 prompt 优化成更完整的版本。失败返回空串（调用方退回正常流程）。"""
    if not (settings.llm_enabled and settings.llm_api_key):
        return ""
    # 去掉显式触发词，只留真正的 prompt 正文
    body = re.sub(r"(优化提示词|优化这个提示词|帮我优化prompt|帮我改提示词|润色提示词|完善提示词)[:：]?",
                  "", text).strip() or text
    sys_prompt = (
        "你是 DramaTV 的提示词优化助手。用户会发来一段用于 AI 生成图片/视频的提示词，"
        "你帮他优化得更完整、更易出好效果。优化标准（DramaTV 规范）：\n"
        "- 角色类：补全年龄、性别、外貌、服装、表情、姿态、场景、画风\n"
        "- 视频/镜头类：补全人物、动作、场景、镜头运动、情绪氛围、画面风格、关键变化\n"
        "规则：只在原意基础上补充完善，不改变用户核心创意；不要编造与原文无关的内容；"
        "输出用中文，不要用 markdown。"
    )
    user_prompt = (
        f"请优化下面这段提示词，按这个格式回复：\n"
        f"【优化后】(给出优化版)\n"
        f"【补充了】(简述补了哪些维度)\n\n"
        f"原提示词：{body}"
    )
    try:
        from . import llm
        r = llm.raw_chat(sys_prompt, user_prompt) if hasattr(llm, "raw_chat") else None
        if r and getattr(r, "text", ""):
            return r.text.strip()
    except Exception as e:
        log.warning("prompt optimize 失败: %s", e)
    return ""
