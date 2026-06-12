#!/usr/bin/env python3
"""
把 DramaTV 完整使用指南作为「学习材料」注入 product_background。

理念：不改成 QA 形式，让 LLM 每次回答时都"读过"完整文档。
就像给机器人一本随身的产品手册。

清理规则：
- 去掉占位标记（> 📷 图片占位 / > 🎬 视频占位 / > 📷 结果占位）
- 去掉分节符 ---
- 去掉目录段（点击标题跳转）
- 保留标题层级 + 全部正文 + 示例 + 代码块

输出：原文档约 25KB → 清理后约 18KB
预计 prompt 增加：+5KB ≈ +1500 tokens
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import SessionLocal, SysSetting  # noqa: E402
from app.services.qa_store import store  # noqa: E402


DOC = Path("/Users/dianzhong/Downloads/DramaTV使用指南_仿LibTV结构草稿.md")
LEARNING_MARKER = "═══ DRAMATV 学习材料 ═══"


def clean_doc(text: str) -> str:
    """清理文档：去占位、去导航、保留所有知识内容。"""
    lines = []
    skip_until_next_heading = False  # 跳过目录段
    for line in text.split("\n"):
        s = line.rstrip()
        # 跳过占位标记
        if s.lstrip().startswith(("> 📷", "> 🎬")):
            continue
        # 跳过分节符
        if s.strip() == "---":
            continue
        # 跳过目录段（"指南目录" 后 ~50 行全是导航）
        if "📖 指南目录" in s or "指南目录（点击标题" in s:
            skip_until_next_heading = True
            continue
        if skip_until_next_heading:
            # 遇到下一个真正章节标题（## 或 ### ）就停止跳过
            if re.match(r"^#{1,3}\s+\d+\.\s*\S", s) or re.match(r"^#{1,3}\s+[一-鿿]", s):
                skip_until_next_heading = False
                lines.append(s)
            continue
        lines.append(s)
    cleaned = "\n".join(lines)
    # 多余空行合并
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


HEADER = """【⚠️ DramaTV 产品完整学习材料 — 必须严格阅读】

下面是 DramaTV 产品的完整使用指南。回答用户关于 DramaTV 的任何问题前，
你必须先把下面的内容当作产品手册读懂。回答严格遵守以下原则：

1. 用户问到 DramaTV 任何功能时，先在下面学习材料里找对应章节
2. 学习材料里**明确写到**的功能 → 可以放心介绍
3. 学习材料里**没写**的功能 → 不要编造，回答 "这块儿我不太确定，建议看下使用指南或问产品支持"
4. 学习材料只是参考，不要把章节编号 / 占位符 / 内部术语原样吐给用户
5. 用佐伊的口语化人设回答，把手册内容翻译成"同事聊天"的语气

═══ DRAMATV 学习材料 ═══

"""

FOOTER = """

═══ 学习材料结束 ═══

【关键反常识结论 — 必须记住】
- DramaTV 不是单点生图/生视频工具，节点 + 连线 + 资产复用 才是核心价值
- 「测试用快速模型 + 成片用高质量模型」是推荐做法（seedance-fast vs kling-video-o1-pro 等）
- 系列化内容必须用资产库，否则风格会漂移
- 「全能参考」适合复杂画面，要同时连接角色图 + 场景图 + 风格图 + 文本节点
- 视频超分用于交付前优化，视频分解用于学习竞品/提取关键帧

【严谨性约束】
- 涉及具体按钮位置、参数数值、积分价格 → 学习材料没写就不要编
- 涉及功能上线状态 → 学习材料是「草稿版」，重大新功能引导用户去问产品支持
- 涉及未在学习材料出现的模型名 → 不要编型号
"""


def main():
    raw = DOC.read_text(encoding="utf-8")
    cleaned = clean_doc(raw)
    body = HEADER + cleaned + FOOTER
    body_len = len(body)
    print(f"原文档: {len(raw)} 字符")
    print(f"清理后: {len(cleaned)} 字符")
    print(f"加包装: {body_len} 字符 (≈ {body_len // 3} tokens)")
    print()

    if body_len > 30000:
        print(f"⚠️  超过 30000 字符（{body_len}），太长会拖慢每次 LLM 调用")
        confirm = input("是否继续？(y/n): ")
        if confirm.lower() != "y":
            print("已取消")
            return

    db = SessionLocal()
    try:
        row = db.get(SysSetting, "product_background")
        cur = (row.v if row else "") or ""
        if LEARNING_MARKER in cur:
            print("⚠️  已经注入过学习材料，跳过（防重复）")
            print(f"   当前长度: {len(cur)} 字符")
            print(f"   要替换请手动清空 product_background 再跑")
            return

        # 备份当前内容到一个临时位置（不入库，写在 logs/）
        if cur.strip():
            bak = Path(BACKEND) / "logs" / "product_background_before_dramatv_learning.txt"
            bak.write_text(cur, encoding="utf-8")
            print(f"📦 旧 product_background 备份到: {bak} ({len(cur)} 字符)")

        # 写入：新材料 + 旧背景内容（保留 1941 字精华版作为额外背景）
        if cur.strip():
            new_v = body + "\n\n【附：业务背景补充】\n" + cur
        else:
            new_v = body
        if row is None:
            db.add(SysSetting(k="product_background", v=new_v))
        else:
            row.v = new_v
        db.commit()
        store.reload()
        print(f"\n✅ 写入 product_background, 总 {len(new_v)} 字符")
        print(f"✅ qa_store 已热重载，下一条用户消息生效")
        print(f"\n=== 验证：开头 300 字 ===")
        print(new_v[:300])
        print(f"\n=== 验证：末尾 300 字 ===")
        print(new_v[-300:])
    finally:
        db.close()


if __name__ == "__main__":
    main()
