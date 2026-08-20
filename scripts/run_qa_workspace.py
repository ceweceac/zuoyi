#!/usr/bin/env python3
"""从本地私密 env 文件启动 QA 测试工作台。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"配置文件不存在：{path}")
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"{path}:{line_no} 不是 KEY=VALUE 格式")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise SystemExit(f"{path}:{line_no} 环境变量名无效")
        os.environ[key] = value.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("backend/.qa-workspace.local.env"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    load_env(args.env_file.resolve())
    backend_dir = Path(__file__).resolve().parents[1] / "backend"
    os.chdir(backend_dir)
    sys.path.insert(0, str(backend_dir))

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
