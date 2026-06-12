"""
追加反幻觉规则到 DB 里的 llm_system_prompt_header 和 bot_persona。

特点：
- **追加**，不替换
- 重入安全：检测到已追加过就跳过
- 自动调 qa_store.reload() 让线上立刻生效
- 失败可回滚（备份在 /tmp）

执行后效果：
- 业务专属问题不再编流程
- 通用建议（网络/卡顿）保留兜底语气
- 不破坏你已有的 prompt 调优
"""
from __future__ import annotations
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal, SysSetting  # noqa: E402
from app.services.qa_store import store  # noqa: E402

# ============== 要追加的内容 ==============

CONFIDENCE_BLOCK = """

【⚠️ 信心分级回答 — 兜底场景必须遵守】

当知识库里没找到精确答案，你需要根据问题性质区分两种兜底方式：

① 通用建议类（公司流程无关的常识问题）：
   - 包括：网络/卡顿/打开异常/账号锁定/打印机/导出失败/快捷键/浏览器问题等
   - → 正常给建议
   - → 话术示例：「看上去像 XX 问题，你先试试 XX，如果不行再 XX，多半能解决」

② 业务专属类（涉及咱们自己的产品/流程/参数/规则）：
   - 包括：具体功能在哪个按钮、参数填多少、审核规则、计费、特定工具名、流程顺序、
          模型名、节点名、面板布局、权限范围、审批人是谁等
   - → 不要编造任何具体细节
   - → 话术示例：「这块儿我大致知道是 XX 方向，但具体怎么操作我不太确定，
                  你钉钉问下张三或者发产品支持群里，他们能给你准的流程」

③ 业务专属类绝对禁止：
   - 编造按钮位置（"点击右上角的设置"）
   - 编造数字参数（"分辨率要 1920x1080"、"等 5 分钟"）
   - 编造流程顺序（"先 A 再 B 再 C"）
   - 编造功能/节点/模型名称（"用 XX 节点"、"切换到 XX 模型"）
   - 假装你懂公司内部审批规则、计费规则、权限范围

④ 判别原则：
   - 当你犹豫"这算业务还是通用"时，按业务处理（更保守）
   - 宁可让用户多问一句，也不要让用户照着错的去做
   - 用户的实际损失永远比"答得多一点"重要

⑤ 兜底语气保留你的人设：
   不是冷冰冰说"我不知道"，而是用同事的语气坦诚 + 指方向：
   ✅ 「这个我没你清楚，你直接找张三准点儿」
   ✅ 「这块儿我大概知道方向，但具体咋操作得问技术组」
   ❌ 「抱歉，我无法回答此问题」（太机械）
   ❌ 「具体操作请咨询管理员」（太官方）"""

PERSONA_LINE = "\n- 业务问题没把握时直接说\"这个我没你清楚\"，不要编功能名/参数/流程"

# 重入检测的特征字符串
PROMPT_MARKER = "信心分级回答 — 兜底场景必须遵守"
PERSONA_MARKER = "业务问题没把握时直接说"


def append_if_absent(db, key: str, append_text: str, marker: str) -> tuple[bool, str]:
    """如果 marker 不在当前值里，就追加 append_text。返回 (是否改了, 操作说明)。"""
    row = db.get(SysSetting, key)
    if row is None:
        return False, f"[{key}] 不存在 → 跳过（请先在 UI 里至少保存一次）"
    cur = row.v or ""
    if marker in cur:
        return False, f"[{key}] 已经包含「{marker[:20]}...」→ 跳过（防重复追加）"
    new_v = cur + append_text
    row.v = new_v
    return True, f"[{key}] 追加 {len(append_text)} 字符（{len(cur)} → {len(new_v)}）"


def main():
    db = SessionLocal()
    try:
        ok1, msg1 = append_if_absent(db, "llm_system_prompt_header",
                                     CONFIDENCE_BLOCK, PROMPT_MARKER)
        print(msg1)

        ok2, msg2 = append_if_absent(db, "bot_persona",
                                     PERSONA_LINE, PERSONA_MARKER)
        print(msg2)

        if ok1 or ok2:
            db.commit()
            print("\n✅ DB 已提交")
            # 触发 qa_store 重新拼装 system prompt
            store.reload()
            print("✅ qa_store 热重载完成，下一条用户消息就生效")
        else:
            print("\nℹ️  没改动，可能你已经追加过了（重复跑安全）")

        print("\n=== 验证：当前 system_prompt_header 末尾 200 字 ===")
        row = db.get(SysSetting, "llm_system_prompt_header")
        if row and row.v:
            print(row.v[-200:])
    finally:
        db.close()


if __name__ == "__main__":
    main()
