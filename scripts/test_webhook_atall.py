#!/usr/bin/env python3
"""测试自定义机器人 Webhook 能否 @全体（加签模式）。"""
import sys
import json
import time
import hmac
import hashlib
import base64
import urllib.parse
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import httpx  # noqa

WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=9e054e5814b8a55979c20402752fc8e7ffe1384304c8dc933e77456bd59e8267"
SECRET = "SEC9d0ddb3c77bef4c85c6d9ddb211faaa1e915bd1598c4a2f3a95d8b39678b449f"


def signed_url() -> str:
    """钉钉加签：URL 拼 timestamp + sign。"""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{SECRET}"
    hmac_code = hmac.new(SECRET.encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in WEBHOOK else "?"
    return f"{WEBHOOK}{sep}timestamp={ts}&sign={sign}"


def send(payload: dict, label: str):
    print(f"\n{'='*60}")
    print(f"测试 [{label}]")
    print(f"payload: {json.dumps(payload, ensure_ascii=False)}")
    url = signed_url()
    r = httpx.post(url, json=payload, timeout=15)
    print(f"HTTP {r.status_code}")
    try:
        data = r.json()
        print(f"响应: {json.dumps(data, ensure_ascii=False)}")
        if data.get("errcode") == 0:
            print("✅ 接口返回成功（去群里看实际 @ 效果）")
        else:
            print(f"❌ errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
    except Exception as e:
        print(f"响应解析失败: {r.text[:300]} ({e})")


if __name__ == "__main__":
    # 测试 1: text 类型 + isAtAll（最标准的 @全体写法）
    send({
        "msgtype": "text",
        "text": {"content": "【Webhook @全体测试1】text类型 + isAtAll"},
        "at": {"isAtAll": True},
    }, "1: text + isAtAll")

    import time; time.sleep(1)

    # 测试 2: markdown 类型 + isAtAll
    send({
        "msgtype": "markdown",
        "markdown": {"title": "@全体测试2", "text": "### Webhook @全体测试2\n\nmarkdown类型 + isAtAll"},
        "at": {"isAtAll": True},
    }, "2: markdown + isAtAll")
