"""提示词优化：用户发来一段创作类 prompt 时，机器人帮优化成更完整的版本。

质量保障（避免"一套指令打天下"的死板）：
- 分场景：识别 prompt 是角色 / 场景 / 视频镜头 / 产品广告 哪一类，套对应专业标准
  （标准来自 DramaTV KB 的提示词规范）。
- 带示范：每个场景给一条「输入→优化后」的标杆示例（few-shot），让 LLM 照着产品
  风格优化，而不是泛泛发挥。
- 识别从严：必须像 prompt（够长 + 创作特征词 + 非疑问句），宁漏不误。
"""
import logging
import re

from ..config import settings

log = logging.getLogger(__name__)

_CREATIVE_HINTS = (
    "画面", "镜头", "角色", "人物", "场景", "风格", "光影", "氛围", "构图",
    "特写", "全景", "近景", "远景", "运镜", "推进", "拉远", "服装", "造型",
    "背景", "色调", "质感", "写实", "动漫", "古风", "赛博", "电影感",
    "穿着", "表情", "姿态", "妆造", "分镜", "提示词", "prompt",
    # 角色/外形词（"白衣剑客站桃花树下"这类纯画面描述也要能识别）
    "剑客", "侠客", "公子", "姑娘", "少年", "少女", "长发", "束发",
    "白衣", "黑衣", "逆光", "桃花", "簪花", "广告", "产品", "海报",
)
_QUESTION_HINTS = (
    "吗", "怎么", "如何", "为什么", "为啥", "是不是", "能不能", "可以吗",
    "?", "？", "怎么办", "什么意思", "哪里", "多少", "几个",
)

# ── 分场景的优化标准 + 标杆示例（few-shot）──
# 标准对齐 DramaTV KB（#467 角色 / #473 视频）。示例给 LLM 看"好的优化长什么样"。
_SCENES = {
    "video": {
        "name": "视频镜头",
        "hints": ("镜头", "运镜", "推进", "拉远", "动作", "奔跑", "转身", "视频", "动态", "追逐", "走出", "走向"),
        "standard": "人物、动作、场景、镜头运动、情绪氛围、画面风格、关键变化（有参考图/前后帧也说明）",
        "example_in": "男主走向女主",
        "example_out": "【优化后】男主一袭白衣长发，神情温润，缓步走向立于竹林小院中的女主；镜头从中景缓慢推进至近景，落叶随风轻旋，光影斑驳。古风仙侠画风，色调清冷淡雅，水墨晕染质感，情绪含蓄克制。\n【补充了】外貌、镜头运动（推进）、场景、光影、画风、情绪",
    },
    "character": {
        "name": "角色设定",
        "hints": ("角色", "人物", "男主", "女主", "穿着", "服装", "造型", "妆造", "表情",
                  "发型", "少年", "少女", "女孩", "男孩", "男人", "女人", "剑客", "侠客",
                  "公子", "姑娘", "老人", "孩子", "长发", "束发", "白衣", "黑衣"),
        "standard": "年龄、性别、外貌特征、服装、表情、姿态、场景、画风",
        "example_in": "白衣剑客",
        "example_out": "【优化后】一位二十岁左右的白衣少年剑客，长发束冠，眉目清冷，腰悬青玉葫芦，身着翩翩白袍，立于桃花树下，神情疏离孤傲。古风仙侠画风，逆光柔和，意境悠远。\n【补充了】年龄、外貌、服装、配饰、姿态、场景、画风、氛围",
    },
    "product": {
        "name": "产品/广告",
        "hints": ("产品", "广告", "手表", "手机", "包装", "品牌", "电商", "海报", "带货"),
        "standard": "产品主体、材质细节、摆放/使用场景、光影、镜头、目标人群、商业质感风格",
        "example_in": "一个手表广告",
        "example_out": "【优化后】一款高端智能手表，银色金属表壳搭配深蓝表盘，置于浅灰大理石台面，侧光勾勒金属质感；镜头由远及近推至表盘特写，再切换至三十岁白领男性手腕佩戴的展示。高对比、干净利落的商业产品摄影风格。\n【补充了】产品细节、材质、场景、光影、镜头、目标人群、商业风格",
    },
}
_DEFAULT_STANDARD = "主体、外貌/细节、场景、光影氛围、画面风格"


def _pick_scene(text: str) -> dict:
    """判断 prompt 属于哪个场景，返回对应的标准+示例配置。"""
    best, best_hits = None, 0
    for cfg in _SCENES.values():
        hits = sum(1 for w in cfg["hints"] if w in text)
        if hits > best_hits:
            best, best_hits = cfg, hits
    return best or {"name": "通用", "standard": _DEFAULT_STANDARD,
                    "example_in": "", "example_out": ""}


def looks_like_prompt(text: str) -> bool:
    """从严判断：这段文本是不是一段待优化的创作 prompt。"""
    if not text:
        return False
    s = text.strip()
    if any(t in s for t in ("优化提示词", "优化这个提示词", "帮我优化prompt",
                            "帮我改提示词", "润色提示词", "完善提示词")):
        return True
    if any(q in s for q in _QUESTION_HINTS):
        return False
    hits = sum(1 for w in _CREATIVE_HINTS if w in s)
    # 三档（非疑问句前提下）：
    # - 短文本(<15字)：需 ≥3 创作词（纯画面短描述如"白衣剑客站桃花树下"）
    # - 中文本(15~24字)：需 ≥2 创作词
    # - 长文本(≥25字)：含 ≥1 创作词即可（够长的陈述句基本在描述画面/镜头）
    if len(s) >= 25:
        return hits >= 1
    if len(s) >= 15:
        return hits >= 2
    return hits >= 3


def optimize(text: str) -> str:
    """分场景 + 标杆示例优化用户 prompt。失败返回空串（调用方退回正常流程）。"""
    if not (settings.llm_enabled and settings.llm_api_key):
        return ""
    body = re.sub(r"(优化提示词|优化这个提示词|帮我优化prompt|帮我改提示词|润色提示词|完善提示词)[:：]?",
                  "", text).strip() or text
    scene = _pick_scene(body)
    example = ""
    if scene.get("example_in"):
        example = (f"\n参考示范（{scene['name']}类）：\n"
                   f"输入：{scene['example_in']}\n输出：\n{scene['example_out']}\n")
    sys_prompt = (
        f"你是 DramaTV 的提示词优化专家，专门优化 AI 生成图片/视频的提示词。"
        f"当前这条属于「{scene['name']}」类，优化时必须补全这些维度：{scene['standard']}。\n"
        f"规则：\n"
        f"1. 只在用户原意基础上补充完善，绝不改变核心创意、不编造无关内容；\n"
        f"2. 优化后的提示词要具体、可直接用于生成，避免空泛形容词堆砌；\n"
        f"3. 用中文，不要 markdown；\n"
        f"4. 严格按示范的格式输出。"
        f"{example}"
    )
    user_prompt = (
        f"请按【优化后】+【补充了】两段格式优化下面这条「{scene['name']}」类提示词：\n\n"
        f"原提示词：{body}"
    )
    try:
        from . import llm
        r = llm.raw_chat(sys_prompt, user_prompt, temperature=0.7, max_tokens=800)
        if r and getattr(r, "text", ""):
            return r.text.strip()
    except Exception as e:
        log.warning("prompt optimize 失败: %s", e)
    return ""
