#!/usr/bin/env python3
"""
测试钉钉企业内部机器人 sampleText 消息 @全体 是否生效。
独立脚本，不动 broadcaster.py。验证通过后再决定怎么集成。

测两种 payload，看哪个能真正 @所有人：
  A) sampleText + msgParam 里带 atAll
  B) sampleText + msgParam 里带 atAll + content 拼 @所有人 文字
"""
import sys
import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.services import runtime_settings  # noqa
from app.config import settings  # noqa
from app.services import broadcaster  # noqa: 复用 _get_access_token
import httpx  # noqa

runtime_settings.load_from_db()

TEST_CONV_ID = "cidUjFuBvOVToZh7A+JFGs2Rw=="   # 群 id=3 刘广伟,燕子
SEND_URL = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"


def send(msg_key: str, msg_param: dict, label: str):
    token = broadcaster._get_access_token()
    robot_code = settings.dingtalk_client_id
    payload = {
        "robotCode": robot_code,
        "openConversationId": TEST_CONV_ID,
        "msgKey": msg_key,
        "msgParam": json.dumps(msg_param, ensure_ascii=False),
    }
    headers = {
        "x-acs-dingtalk-access-token": token,
        "Content-Type": "application/json",
    }
    print(f"\n{'='*60}")
    print(f"测试 [{label}]")
    print(f"msgKey={msg_key}")
    print(f"msgParam={json.dumps(msg_param, ensure_ascii=False)}")
    r = httpx.post(SEND_URL, json=payload, headers=headers, timeout=15)
    print(f"HTTP {r.status_code}")
    try:
        data = r.json()
        print(f"响应: {json.dumps(data, ensure_ascii=False)}")
        ok = bool(data.get("processQueryKey")) and data.get("errcode") in (0, None)
        print("✅ 发送成功" if ok else "❌ 发送失败")
    except Exception as e:
        print(f"响应解析失败: {r.text[:300]} ({e})")


if __name__ == "__main__":
    print(f"目标群: {TEST_CONV_ID}")
    print(f"robotCode: {settings.dingtalk_client_id}")

    import time

    # 方案 C: sampleMarkdown + atAll 字段
    send("sampleMarkdown",
         {"title": "@全体测试C", "text": "### @全体测试 C\n\nmarkdown + atAll 字段", "atAll": True},
         "C: sampleMarkdown + atAll")
    time.sleep(1)

    # 方案 D: sampleMarkdown + atAll + text 拼 @所有人
    send("sampleMarkdown",
         {"title": "@全体测试D", "text": "@所有人\n\n### @全体测试 D\n\ntext拼@所有人 + atAll", "atAll": True},
         "D: sampleMarkdown + atAll + text拼@所有人")
    time.sleep(1)

    # 方案 E: sampleMarkdown + isAtAll（注意是 isAtAll 不是 atAll）
    send("sampleMarkdown",
         {"title": "@全体测试E", "text": "@所有人\n\n### @全体测试 E\n\ntext拼@所有人 + isAtAll", "isAtAll": True},
         "E: sampleMarkdown + isAtAll + text拼@所有人")
