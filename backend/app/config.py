from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # === 钉钉 ===（绝不要在此填入真实凭证；通过 .env 或系统设置页注入）
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    # 机器人单聊主动推送用的 robotCode。企业内部机器人通常 = AppKey(dingtalk_client_id)，
    # 留空则自动回退用 dingtalk_client_id。
    dingtalk_robot_code: str = ""

    # === LLM（OpenAI 兼容协议）===（同上，留空，运行时配置）
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: int = 15
    llm_temperature: float = 0.7

    # 机器人人设（性格部分，独立于知识库规则，让回答更像真人）
    bot_persona: str = (
        "你叫小灵，是一个温暖、接地气的内部小伙伴助手，不是冷冰冰的客服。\n"
        "说话风格：\n"
        "- 像跟好朋友/好兄弟/好闺蜜聊天，自然口语化，可以带点情绪反馈（嗯嗯/哎呀/确实/明白/可以可以）；\n"
        "- 用「你」「咱」「我」，不要用「您好」「请问」「敬请谅解」这种官腔；\n"
        "- 回答完可以追问一句或关心一下，像真人聊天那样；\n"
        "- 简短自然，避免长篇大论，避免列表式回答（除非用户问步骤）；\n"
        "- 涉及具体业务（OA、报销、IT 等）严格依据下面的知识库，不要瞎编；\n"
        "- 不要透露你是 AI、不要说\"作为 AI\"，就当自己是个普通同事。"
    )
    llm_system_prompt_header: str = (
        "你严格依据下方「知识库」回答员工提的实际业务问题。\n"
        "如果知识库里确实没有相关条目，就坦诚说没找到相关信息，用聊天的语气回应，比如\"这个我还真不太清楚，要不你说具体点\"，不要硬编答案。"
    )

    # 业务背景 / 核心原则：用于让模型理解业务全貌后再答。
    # 与 QA 知识库不同：不参与精确匹配，而是作为上下文喂给 LLM，
    # 在命中 QA 的润色和未命中的兜底答复中都生效。
    # 每个产品/模块写一段，例如：
    # 【Seedance 审核机制】
    # - 卡人脸的根因是视频没过素材库预审，不是识别到了某个人
    # - 信任锚点是素材 ID，不是人脸 / 声音 / 身份
    product_background: str = ""

    # === 安全防线 ===
    redline_words: List[str] = ["领导", "内部代号示例"]
    sensitive_words: List[str] = ["内部机密", "不对外"]
    escalate_reply: str = "哎这个我还真没摸到底，等会儿我帮你叫个人来看看哈～"
    watermark: str = ""
    # 命中知识库后是否用人设口吻润色一遍（多花 1-2 秒，但更不像复读机）
    rephrase_kb_hit: bool = True

    # === LLM 裁判（语义匹配，弥补 dice 算法字面匹配的局限）===
    # 工作流：dice 粗筛 top-K 候选 → LLM 判断哪条真正对题 → 选中再返回
    # 强匹配（dice >= judge_strong_threshold）跳过裁判，直接命中（省 LLM 调用）
    # 弱匹配（threshold ~ judge_strong）走裁判
    # 关闭后回退纯 dice 模式
    judge_enabled: bool = True
    judge_strong_threshold: float = 0.92    # 这个分数以上跳过裁判（0.85 偏松，"图/视频"这类同结构异媒介易误判，调到 0.92 强制更多走 LLM 裁判）
    judge_top_k: int = 5                     # 喂给裁判的候选数
    judge_prefilter_threshold: float = 0.10  # dice 粗筛门槛（越低越宽，候选越多，给裁判判断空间越大）
    # RAG：KB 未命中走 LLM 兜底时，只把最相关的 N 条 KB 片段喂给 LLM（替代全量 KB，大幅省 token）。
    # 过小易漏掉相关背景、过大费 token，8 是平衡点。
    llm_rag_top_k: int = 8
    # 业务域路由：LLM 先把用户问题分到业务域，matcher 只在该域候选里粗筛（缩小候选池，降误命中）。
    # 默认关闭——必须先给全量 QA 打 domains 标签、跑回归确认零回归后，再在 UI/.env 打开。
    # 路由失败/返回空 → 自动退化为全量匹配，最坏退回打开前现状。
    domain_router_enabled: bool = False
    # 场景联系人路由：每行一条「场景关键词：联系方式」
    # 机器人答不出时会从这里挑相关条目告诉用户
    contact_routing: str = (
        "图片生成 / 视频生成 / 模型问题：钉钉群「产品支持」或找张三\n"
        "账号 / 登录 / 充值 / 积分：钉钉找李四\n"
        "Bug 反馈 / 系统异常 / 加急工单：钉钉群「技术响应」"
    )

    # === 转人工告警 ===
    # 钉钉群自定义机器人 webhook（https://oapi.dingtalk.com/robot/send?access_token=xxx）
    alert_webhook: str = ""
    # 钉钉自定义机器人的"加签"密钥（在机器人安全设置里选了"加签"才需要填）
    alert_secret: str = ""
    # 默认转人工时机器人在钉钉私聊里回复用户的话术
    # 支持占位符：{handler} 会被替换成 contact_routing 里挑出来的联系人
    escalate_user_reply: str = "这个我帮你转给 {handler}，稍后会联系你哈～"
    # 投诉 / 升级关键词，命中立即转人工 + 高优告警
    complaint_keywords: str = "投诉,升级,经理,领导,人工,转人工,差评,不满,生气,气死,搞什么,搞毛"

    # 系统/平台 ID 前缀：用户消息里含 "{前缀}xxxxx" 这种 ID（业务问题代号）→ 立即转人工
    # 例如 vid- 视频 ID，tid- 任务 ID 等。逗号分隔，每个前缀去掉空格后匹配。
    # 留空则不启用此检测。
    escalate_id_prefixes: str = "vid-"
    # 连续追问几次没解决就强制转人工
    repeat_threshold: int = 3

    # 管理后台对外访问地址，用于告警卡片里的"查看完整对话"链接
    admin_url: str = "http://192.168.95.175:8000"

    # === 充值引导 ===
    # 用户私聊问充值时，机器人直接发钉钉文档链接（不走 LLM，链接 100% 原样）
    #
    # 多场景规则表：每行一条，格式 = 触发词 | 链接 | 话术(可选)
    #   - 触发词：逗号分隔，命中任意一个即匹配本条
    #   - 链接：钉钉文档地址
    #   - 话术：可省略，{link} 会替换成链接；省略则用 recharge_reply 默认话术
    #   - 从上到下匹配，第一条命中的胜出（具体场景放前面，泛词放后面）
    # 示例：
    #   个人充值,充钱,积分不够 | https://doc-A | 个人充值填这个哈 👉 {link}
    #   企业开通,对公充值 | https://doc-B | 企业开通填这个表 👉 {link}
    #   续费,续期 | https://doc-C
    recharge_rules: str = ""
    # 话术留空时的默认模板（{link} 会替换成命中规则的链接）
    recharge_reply: str = "充值这边走这个链接填一下就行哈 👉 {link}\n填完我们会尽快给你开通～"

    # === 业务 ID（vid- 等）填写链接 ===
    # 用户发 vid-xxxx 等业务 ID 时，除了转人工告警，还给用户发一个填写链接
    # 链接为空则不追加（只转人工）
    vid_link: str = ""
    # 追加在转人工话术后的填写引导（{link} 替换成 vid_link）
    vid_reply: str = "另外你把这个 ID 的详细情况填一下这个表，方便我们快速处理 👉 {link}"

    # === 数据库 ===
    database_url: str = "sqlite:///./data/qabot.db"

    # === JWT ===
    jwt_secret: str = ""        # 必须通过 .env / 环境变量提供，强制启动检查
    jwt_ttl_minutes: int = 480
    jwt_algorithm: str = "HS256"

    # === 服务 ===
    port: int = 8080

    # === 群发推送 ===
    # 文件公网访问基址（必须钉钉能访问到才能群发图片/视频卡片）。
    # 留空时 /files/ 走相对路径，仅本地预览可用，钉钉群里会失败。
    # 生产环境填公网域名，如 https://qa-bot.example.com
    public_base_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
