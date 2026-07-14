"""提示词优化：用户发来一段创作类 prompt 时，机器人帮优化成更完整的版本。

质量保障（避免"一套指令打天下"的死板）：
- 分场景：识别 prompt 是角色 / 场景 / 视频镜头 / 产品广告 哪一类，套对应专业标准
  （标准来自 DramaTV KB 的提示词规范）。
- 带示范：每个场景给一条「输入→优化后」的标杆示例（few-shot），让 LLM 照着产品
  风格优化，而不是泛泛发挥。
- 识别从严：必须像 prompt（够长 + 创作特征词 + 非疑问句），宁漏不误。
"""
import json
import logging
import re
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)

# 专业素材库（光影/运镜/情绪等官方提示词表述，来源「功能提示词收集」文档）。
# 优化时若用户 prompt 涉及这些专业概念，注入对应官方描述，让 LLM 用专业表述而非自编。
_MATERIALS = {}
try:
    _mat_path = Path(__file__).resolve().parent / "prompt_materials.json"
    if _mat_path.exists():
        _MATERIALS = json.loads(_mat_path.read_text(encoding="utf-8"))
except Exception as _e:
    logging.getLogger(__name__).warning("加载 prompt_materials 失败: %s", _e)


def _match_materials(text: str, limit: int = 3) -> str:
    """从用户 prompt 里找命中的专业概念，返回对应官方描述（最多 limit 条）。"""
    if not _MATERIALS:
        return ""
    hits = []
    for name, desc in _MATERIALS.items():
        # 概念名出现在 prompt 里（如"逆光""推镜头""伦勃朗"）
        key = name.split("/")[0]
        if key and key in text:
            hits.append(f"- {name}：{desc}")
        if len(hits) >= limit:
            break
    return "\n".join(hits)


# ── 官方场景指令库（26条专家级 system prompt，来源「预设提示词分类整理」）──
# 用户发简短描述 → 识别用途+子分类 → 用对应官方指令优化。
_OFFICIAL_SCENES = []
try:
    _sc_path = Path(__file__).resolve().parent / "optimizer_scenes.json"
    if _sc_path.exists():
        _OFFICIAL_SCENES = json.loads(_sc_path.read_text(encoding="utf-8"))
except Exception as _e:
    logging.getLogger(__name__).warning("加载 optimizer_scenes 失败: %s", _e)

# 用途关键词（判断文生文/图/视频/图生图）
_USE_HINTS = {
    "文生视频": ("视频", "镜头", "运镜", "动作场面", "追逐", "打斗", "短片", "动态", "秒"),
    "图生图": ("这张图", "原图", "参考图", "风格迁移", "换风格", "调色", "色调", "推演", "改成"),
    "文生图": ("画", "图", "人物", "角色", "场景", "海报", "画风", "氛围", "特效", "全景", "宫格"),
    "文生文": ("文案", "改写", "润色", "扩写", "压缩", "口播", "台词", "塑造", "写实角色"),
}
# 子分类关键词（命中则优先选该子分类的官方指令）
_SUB_HINTS = {
    "人物/角色": ("人物", "角色", "男主", "女主", "剑客", "少年", "少女", "人设"),
    "场景/环境": ("场景", "环境", "街道", "森林", "房间", "城市", "空间"),
    "情节/动作": ("动作", "奔跑", "打斗", "追逐", "情节"),
    "画风/风格": ("画风", "风格", "水墨", "赛博", "二次元", "写实风"),
    "氛围": ("氛围", "情绪", "意境"),
    "特效": ("特效", "光效", "粒子", "爆炸", "能量"),
    "动作场面": ("动作场面", "打斗", "追逐", "武打"),
    "情感场景": ("情感", "对话", "哭", "拥抱", "告白"),
    "叙事场景": ("叙事", "剧情", "故事", "冲突"),
    "氛围短片": ("氛围短片", "意境", "唯美"),
    "风格迁移": ("风格迁移", "换风格", "迁移"),
    "色调调整": ("色调", "调色", "影调"),
    "四宫格": ("四宫格", "4宫格", "4格"),
    "九宫格": ("九宫格", "9宫格", "9格"),
    "25宫格连贯分镜": ("25宫格", "分镜"),
    "全景生成": ("全景", "720", "vr"),
    "电影级光影校正": ("光影校正", "打光", "光影", "逆光", "伦勃朗"),
    "内容改写": ("改写", "润色", "扩写", "压缩文案"),
    "场景描述": ("场景描述",),
    "人物塑造": ("人物塑造", "人设文案"),
}


def _find_scene(use, sub_keyword):
    """按 use(可空) + 子分类名包含 sub_keyword 找官方场景。"""
    for sc in _OFFICIAL_SCENES:
        if (not use or sc["use"] == use) and sub_keyword in sc["sub"]:
            return sc
    # 不限用途再找一次
    for sc in _OFFICIAL_SCENES:
        if sub_keyword in sc["sub"]:
            return sc
    return None


# 强信号 → 直接定位场景（优先级最高，不参与通用竞争）。
# 这些是明确的"特殊格式/操作"，关键词出现即确定，避免被"人物/场景"等通用词抢。
_STRONG_SIGNALS = [
    # (关键词组, use, 子分类keyword)  —— 顺序敏感，先匹配先生效
    (("25宫格", "二十五宫格"), None, "25宫格"),
    (("九宫格", "9宫格", "9格"), "文生文", "九宫格"),
    (("四宫格", "4宫格", "4格"), "文生图", "四宫格"),
    (("全景", "720", "vr全景", "panorama"), "图生图", "全景"),
    (("光影校正", "电影级光影"), "图生图", "光影校正"),
    (("3秒前", "三秒前", "推演前", "往前推"), "图生图", "3秒前"),
    (("5秒后", "五秒后", "推演后", "往后推"), "图生图", "5秒后"),
    # 图生图操作类（"换/迁移/调色/改成"+图）
    (("风格迁移", "迁移风格", "换风格", "换成", "改成"), "图生图", "风格迁移"),
    (("调色", "色调", "调成", "影调"), "图生图", "色调调整"),
]


# 视频语境信号：出现这些说明是"多镜头视频/分镜脚本"，而非单图生成。
# 用于给"全景"等图片类强信号加护栏——"全景"在视频里是景别术语(和近景/远景并列)，
# 不代表要生成 720°VR 全景图。
_VIDEO_CONTEXT = (
    "镜头", "运镜", "长镜头", "第一镜", "第二镜", "第三镜", "第四镜", "第五镜",
    "分镜", "镜，", "一镜", "近景", "远景", "中景", "特写", "推镜", "拉镜",
    "跟随", "俯拍", "仰拍", "低角度", "短片", "视频", "动态",
)


def _looks_like_video(s: str) -> bool:
    """是否明显是视频/分镜脚本：≥2 个视频语境信号即判定。"""
    return sum(1 for w in _VIDEO_CONTEXT if w in s) >= 2


# 视频语境下应让位、不该抢跑的图片类强信号子类（这些本质是"生成一张图"的特殊格式）。
_IMAGE_ONLY_STRONG_SUBS = {"25宫格", "九宫格", "四宫格", "全景",
                           "光影校正", "3秒前", "5秒后",
                           "风格迁移", "色调调整"}


def _pick_official_scene(text: str):
    """从用户描述识别官方场景。返回 scene dict 或 None（让上层退回内置场景）。"""
    if not _OFFICIAL_SCENES:
        return None
    s = text.lower()
    is_video = _looks_like_video(s)

    # 0) 强信号优先：特殊格式/操作类，命中即定位。
    #    但若明显是视频脚本，图片类强信号(全景/宫格/调色等)让位——避免"全景"这种
    #    景别词把多镜头视频脚本误判成 720°VR 单图生成。
    for keys, use, sub_kw in _STRONG_SIGNALS:
        if any(k in s for k in keys):
            if is_video and sub_kw in _IMAGE_ONLY_STRONG_SUBS:
                continue
            sc = _find_scene(use, sub_kw)
            if sc:
                return sc

    # 1) 定用途（图生图信号优先——有"图/原图/参考图/这张"且含操作词时）
    use = None
    for u, hints in _USE_HINTS.items():
        if any(h in s for h in hints):
            use = u
            break

    # 2) 在该用途下按子分类命中数选最高
    cands = [sc for sc in _OFFICIAL_SCENES if (not use or sc["use"] == use)]
    best, best_hits = None, 0
    for sc in cands:
        hints = _SUB_HINTS.get(sc["sub"], (sc["sub"],))
        hits = sum(1 for h in hints if h in s)
        if hits > best_hits:
            best, best_hits = sc, hits
    if best and best_hits >= 1:
        return best
    # 3) 子分类没命中，但定了用途 → 用该用途第一条兜底
    if use and cands:
        return cands[0]
    # 4) 关键词完全没命中 → LLM 兜底分类（覆盖"老奶奶/柴犬/跑车"等关键词追不全的情况）
    return _llm_classify_scene(text)


def _llm_classify_scene(text: str):
    """关键词识别不到时，让 LLM 从26场景里选一个，返回 scene dict 或 None。"""
    if not (settings.llm_enabled and settings.llm_api_key) or not _OFFICIAL_SCENES:
        return None
    menu = "\n".join(f"{i}. {sc['use']}/{sc['sub']}" for i, sc in enumerate(_OFFICIAL_SCENES))
    sys_p = (
        "你是提示词场景分类器。用户给一段创作描述，你判断它最适合下面哪个优化场景，"
        "只输出对应的数字编号（0-25），不要任何解释。\n场景列表：\n" + menu
    )
    try:
        from . import llm
        r = llm.raw_chat(sys_p, f"创作描述：{text}", temperature=0, max_tokens=10)
        if r and getattr(r, "text", ""):
            m = re.search(r"\d+", r.text)
            if m:
                idx = int(m.group())
                if 0 <= idx < len(_OFFICIAL_SCENES):
                    log.info("LLM 场景分类: %s → %s", text[:20], _OFFICIAL_SCENES[idx]["sub"])
                    return _OFFICIAL_SCENES[idx]
    except Exception as e:
        log.warning("LLM 场景分类失败: %s", e)
    return None

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
    hits = sum(1 for w in _CREATIVE_HINTS if w in s)
    # 疑问词否决：只对短/中文本生效。长分镜脚本（"…石台上，双眼紧闭？…"这类
    # 内嵌一个问号的创作描述）不该因单个问号被当成提问而拒之门外——用创作词密度兜底：
    # 长文本(≥40字)且创作词很多(≥4)时，即便含问号也按 prompt 处理。
    is_long_creative = len(s) >= 40 and hits >= 4
    if not is_long_creative and any(q in s for q in _QUESTION_HINTS):
        return False
    # 三档（非疑问句 / 长创作文本前提下）：
    # - 短文本(<15字)：需 ≥3 创作词（纯画面短描述如"白衣剑客站桃花树下"）
    # - 中文本(15~24字)：需 ≥2 创作词
    # - 长文本(≥25字)：含 ≥1 创作词即可（够长的陈述句基本在描述画面/镜头）
    if len(s) >= 25:
        return hits >= 1
    if len(s) >= 15:
        return hits >= 2
    return hits >= 3


# 引用锚点保留约束：@[图片N] / @[音频N] 是用户绑定的参考素材锚点（哪一镜用哪张图/哪段音），
# 是脚本的关键语义，优化时 LLM 绝不能删改或改写，否则整条 prompt 失去可用性。
_REF_KEEP_RULE = (
    "\n【最重要·引用锚点】用户文本里形如 〖REFk〗 的记号是绑定的参考素材/音频占位符，"
    "**必须原样逐字保留**在它所在的镜次/位置上，**绝不允许删除、改写、合并、翻译或改变其中的数字**。"
    "即使你在精简或合并句子，也**不得删掉任何一个 〖REFk〗**——每一个都对应用户的一张参考图或一段音频，"
    "少一个都会让脚本失效。逐一核对：输入有几个 〖REF…〗，输出就必须有几个，一个都不能少。\n"
)


def _ref_anchors(text: str):
    """返回文本里所有 @[图片N]/@[音频N] 锚点的多重集合(按出现次数计)。"""
    from collections import Counter
    return Counter(re.findall(r"@\[(?:图片|音频)\d+\]", text or ""))


def _has_ref_anchor(text: str) -> bool:
    return bool(re.search(r"@\[(图片|音频)\d+\]", text or ""))


def _anchors_preserved(src: str, out: str) -> bool:
    """校验优化输出 out 是否完整保留了 src 里的每个引用锚点(数量不少于原文)。"""
    src_a = _ref_anchors(src)
    if not src_a:
        return True
    out_a = _ref_anchors(out)
    return all(out_a.get(k, 0) >= v for k, v in src_a.items())


# 锚点哨兵：用 LLM 几乎不会改动的稳定记号占位，优化后再还原。
# 比"命令 LLM 逐字保留"可靠得多——它怎么改写都碰不到真锚点。
_SENTINEL_RE = re.compile(r"〖REF(\d+)〗")


def _protect_anchors(text: str):
    """把 @[图片N]/@[音频N] 逐个替换成 〖REFk〗 哨兵，返回 (替换后文本, {哨兵: 原锚点})。"""
    mapping = {}
    def _sub(m):
        k = len(mapping)
        token = f"〖REF{k}〗"
        mapping[token] = m.group(0)
        return token
    protected = re.sub(r"@\[(?:图片|音频)\d+\]", _sub, text)
    return protected, mapping


def _restore_anchors(text: str, mapping: dict) -> str:
    """把哨兵还原成原锚点；若 LLM 删掉了某些哨兵(整句精简)，把丢失的原锚点
    按序补一份「参考素材清单」到末尾，保证锚点一个不少。"""
    if not mapping:
        return text
    missing = []
    for token, anchor in mapping.items():
        if token in text:
            text = text.replace(token, anchor)
        else:
            missing.append(anchor)
    if missing:
        # 去重保序，补一份清单，避免用户丢失参考图/音频引用
        seen = set(); uniq = []
        for a in missing:
            if a not in seen:
                seen.add(a); uniq.append(a)
        log.warning("optimize: LLM 删除了 %d 个引用锚点，已在末尾补回清单", len(missing))
        text = text.rstrip() + "\n\n【引用素材】" + "、".join(uniq)
    return text


# 分镜镜次标记：第一镜/第1镜/镜头1/分镜3 等。用于判断"结构化多镜头脚本"。
_SHOT_MARK = re.compile(
    r"第[一二三四五六七八九十\d]+镜|第[一二三四五六七八九十\d]+个?镜头|镜头[一二三四五六七八九十\d]+|分镜[一二三四五六七八九十\d]+")


def _is_storyboard(text: str) -> bool:
    """是否是结构化多镜头分镜脚本：≥2 个镜次标记 且 含 ≥3 个参考锚点。
    这类脚本每一镜是独立单元，不能被整体改写/合并，只能逐镜保结构润色。"""
    shots = len(_SHOT_MARK.findall(text))
    anchors = sum(_ref_anchors(text).values())
    return shots >= 2 and anchors >= 3


def _split_shots(body: str):
    """把分镜脚本按镜次标记切成有序段落：[(标记, 内容), ...]。
    标记 '__intro__' 表示第一个镜次之前的序言(核心场景描述)。"""
    parts = re.split(r"(第[一二三四五六七八九十\d]+镜|镜头[一二三四五六七八九十\d]+|分镜[一二三四五六七八九十\d]+)", body)
    segs = []
    if parts and parts[0].strip():
        segs.append(("__intro__", parts[0]))
    for i in range(1, len(parts), 2):
        mark = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        segs.append((mark, content))
    return segs


_SB_SYS_INTRO = (
    "你是 DramaTV 分镜脚本优化助手。下面是一段视频脚本的**开头场景描述**（不是完整脚本）。"
    "请只在原意基础上把画面/氛围/人物细节补充得更具体，适合 AI 生视频。\n"
    "【硬规则】① 文中形如 〖REFk〗 的记号必须原样逐字保留、位置不动、绝不删改或改数字"
    "（每个都是用户绑定的参考图/音频）；② 不要新增/删减内容之外的镜次编号；"
    "③ 保留台词、器材名(ALEXA/Cooke等)照写；④ 中文，不要 markdown，只输出润色后的这段文字。"
)
_SB_SYS_SHOT = (
    "你是 DramaTV 分镜脚本优化助手。下面是视频脚本里**单独一镜**的内容。"
    "请只润色这一镜：把景别、镜头运动、人物动作/外貌、光影氛围补充得更具体，适合 AI 生视频。\n"
    "【硬规则】① 文中形如 〖REFk〗 的记号必须原样逐字保留、位置不动、绝不删改或改数字"
    "（每个都是用户绑定的参考图/音频）；② 这是单独一镜，**不要拆成多镜、不要加镜次编号**；"
    "③ 保留原有台词、声音描述、器材名(ALEXA/Cooke等)照写；④ 中文，不要 markdown，"
    "只输出这一镜润色后的内容本身（不要重复镜次标题）。"
)


def _polish_segment(seg_text: str, is_intro: bool) -> str:
    """润色单个段落(序言或一镜)。锚点哨兵保护+还原；失败/丢锚点则原样返回该段。"""
    from . import safety_guard, llm
    if not seg_text.strip():
        return seg_text
    protected, amap = _protect_anchors(seg_text)
    sys_p = (_SB_SYS_INTRO if is_intro else _SB_SYS_SHOT) + safety_guard.SAFETY_RULES
    try:
        r = llm.raw_chat(sys_p, protected, temperature=0.5, max_tokens=600)
        if r and getattr(r, "text", ""):
            # 只做哨兵→锚点的直接还原(不在段中补清单，避免镜次里插入突兀的【引用素材】)
            txt = r.text.strip()
            for token, anchor in amap.items():
                txt = txt.replace(token, anchor)
            # 单段兜底：润色后锚点数必须≥原段，否则整段降级用原文
            # (逐镜切分段落很小，降级单段损失也小，且保证镜次内锚点原位不缺)
            if _anchors_preserved(seg_text, txt) and "〖REF" not in txt:
                return txt
            log.warning("storyboard 单段锚点不全，保留原段")
    except Exception as e:
        log.warning("storyboard 单段润色失败，保留原段: %s", e)
    return seg_text


def _optimize_storyboard(body: str) -> str:
    """A 方案：代码按镜次切分，逐段(序言+每一镜)独立润色再原样拼回。
    镜次结构与锚点由代码保证——LLM 只在单段内润色，跑不偏；单段失败降级为原段。"""
    segs = _split_shots(body)
    if len(segs) < 2:
        return ""   # 没切出多段，交给常规流程
    out_parts = []
    for mark, content in segs:
        polished = _polish_segment(content, is_intro=(mark == "__intro__"))
        if mark == "__intro__":
            out_parts.append(polished.strip())
        else:
            # 原样拼回镜次标记 + 润色内容，保证镜次结构/顺序不变
            out_parts.append(f"{mark}{polished}")
    result = "\n".join(p for p in out_parts if p.strip())
    log.info("prompt optimize via storyboard(逐镜切分): 段数=%d 锚点=%d/%d",
             len(segs), sum(_ref_anchors(result).values()), sum(_ref_anchors(body).values()))
    # 调用方(pipeline)会加"帮你把提示词完善了一版"前缀，这里只返回正文
    return result if result.strip() else ""


def optimize(text: str) -> str:
    """分场景 + 标杆示例优化用户 prompt。失败返回空串（调用方退回正常流程）。"""
    if not (settings.llm_enabled and settings.llm_api_key):
        return ""
    body = re.sub(r"(优化提示词|优化这个提示词|帮我优化prompt|帮我改提示词|润色提示词|完善提示词)[:：]?",
                  "", text).strip() or text

    # A 方案：结构化多镜头分镜脚本 → 走逐镜保结构润色，绝不整体改写/合并镜次。
    if _is_storyboard(body):
        sb = _optimize_storyboard(body)
        if sb:
            return sb
        # 逐镜优化失败 → 继续走下面常规流程兜底

    # 锚点保护：优化前把 @[图片N]/@[音频N] 换成哨兵，喂给 LLM 的是哨兵版；
    # 场景识别仍用原 body（不受影响）。LLM 输出后再把哨兵还原成真锚点。
    has_anchor = _has_ref_anchor(body)
    body_llm, anchor_map = _protect_anchors(body) if has_anchor else (body, {})

    from . import safety_guard
    ref_rule = _REF_KEEP_RULE if has_anchor else ""
    # 优先用官方场景指令（26条专家级），命中则直接用它优化，质量最高
    official = _pick_official_scene(body)
    if official:
        sys_prompt = (
            official["system"]
            + "\n\n【输出要求】先给出优化后的成品提示词，再用一行『补充了：』简述补充了哪些维度。"
              "用中文，不要 markdown。"
            + ref_rule
            + safety_guard.SAFETY_RULES
        )
        # 喂给 LLM 的是哨兵版（锚点已被 〖REFk〗 占位保护）
        user_prompt = f"用户输入：{body_llm}\n\n请按上面的角色与任务优化。"
        try:
            from . import llm
            r = llm.raw_chat(sys_prompt, user_prompt, temperature=0.7, max_tokens=1200)
            if r and getattr(r, "text", ""):
                out = _restore_anchors(r.text.strip(), anchor_map)
                log.info("prompt optimize via official scene: %s/%s", official["use"], official["sub"])
                return out
        except Exception as e:
            log.warning("official optimize 失败，退回内置: %s", e)

    # 兜底：官方场景没命中 → 用内置 3 类
    scene = _pick_scene(body)
    example = ""
    if scene.get("example_in"):
        example = (f"\n参考示范（{scene['name']}类）：\n"
                   f"输入：{scene['example_in']}\n输出：\n{scene['example_out']}\n")
    # 命中的专业素材（光影/运镜/情绪官方表述）→ 注入，让优化用专业术语
    materials = _match_materials(body)
    mat_block = (f"\n参考专业表述（命中的概念，优化时可融入）：\n{materials}\n"
                 if materials else "")
    sys_prompt = (
        f"你是 DramaTV 的提示词优化专家，专门优化 AI 生成图片/视频的提示词。"
        f"当前这条属于「{scene['name']}」类，优化时必须补全这些维度：{scene['standard']}。\n"
        f"规则：\n"
        f"1. 只在用户原意基础上补充完善，绝不改变核心创意、不编造无关内容；\n"
        f"2. 优化后的提示词要具体、可直接用于生成，避免空泛形容词堆砌；\n"
        f"3. 用中文，不要 markdown；\n"
        f"4. 严格按示范的格式输出。"
        f"{example}{mat_block}"
        + ref_rule
        + safety_guard.SAFETY_RULES
    )
    user_prompt = (
        f"请按【优化后】+【补充了】两段格式优化下面这条「{scene['name']}」类提示词：\n\n"
        f"原提示词：{body_llm}"
    )
    try:
        from . import llm
        # 含引用锚点的长脚本要"保留原文+扩写"，给足 token 上限避免截断把尾部内容切掉。
        max_toks = 1200 if has_anchor else 800
        r = llm.raw_chat(sys_prompt, user_prompt, temperature=0.7, max_tokens=max_toks)
        if r and getattr(r, "text", ""):
            return _restore_anchors(r.text.strip(), anchor_map)
    except Exception as e:
        log.warning("prompt optimize 失败: %s", e)
    return ""
