# -*- coding: utf-8 -*-
r"""查询硅基流动可用对话模型清单（E7 · 小型快模型选型侦察）。

用途：拉取 https://api.siliconflow.cn/v1/models?sub_type=chat 的实时模型列表，
按关键词筛出 4B~14B 级小模型候选（延迟对比实验的备选池）。
Key 从 AI-Customer-Service/.env 的 EMBEDDING_API_KEY 读取（硅基流动同一把 key）。

运行：
    & "E:\毕设_记忆系统\.venv\Scripts\python.exe" -X utf8 `
        "evaluation/spikes/e7_gate/list_sf_models.py"
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[3]


def load_key() -> str:
    env_path = BASE_DIR / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("EMBEDDING_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("EMBEDDING_API_KEY not found in .env")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    key = load_key()
    req = urllib.request.Request(
        "https://api.siliconflow.cn/v1/models?sub_type=chat",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)

    ids = sorted(m["id"] for m in data.get("data", []))
    print(f"== 硅基流动 chat 模型共 {len(ids)} 个 ==\n")
    for i in ids:
        print(" ", i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
