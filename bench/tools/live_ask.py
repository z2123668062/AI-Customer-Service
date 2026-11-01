"""对灌好的记忆库提问，走真实 HTTP 链路（分诊 → 检索 → 精筛 → 生成）。

每题用全新 session_id：窗口为空，答得出来只能靠长期记忆。
"""
from __future__ import annotations

import json
import sys
import uuid

import requests

BASE = "http://127.0.0.1:8000/api/v1/chat/"

QUESTIONS = [
    "我在做什么毕设",
    "我用什么数据集评测，多少道题",
    "我的正确率是多少",
    "我为什么把图传播回滚了",
    "我的项目放在哪个目录",
    "我的向量库跑在哪个端口",
    "我生成和 embedding 分别用什么",
    "答辩之前我只打算主张哪几件事",
    "记忆卡片的写入成本是多少",
    "我对说话方式有什么要求",
]


def ask(question: str) -> dict:
    sid = "s_ask_" + uuid.uuid4().hex[:8]
    payload = {"session_id": sid, "message": question}
    reply, cards, raws, status = "", [], [], []
    with requests.post(BASE, json=payload, stream=True, timeout=300) as r:
        r.encoding = "utf-8"
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            name, content = ev.get("event"), ev.get("content")
            if name == "message":
                reply += content or ""
            elif name == "meta" and isinstance(content, dict):
                cards = content.get("cards") or []
                raws = content.get("raws") or []
            elif name == "status":
                status.append(content)
    return {"q": question, "a": reply.strip(), "cards": cards, "raws": raws}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for i, q in enumerate(QUESTIONS, 1):
        try:
            r = ask(q)
        except Exception as exc:
            print(f"{i}. {q}\n   [请求失败] {exc}\n")
            continue
        print(f"{i}. {q}")
        print(f"   A: {r['a']}")
        src = "、".join(c.get("preview", "")[:18] for c in r["cards"][:4])
        print(f"   注入 {len(r['cards'])} 卡 + {len(r['raws'])} 原文 | {src}\n")


if __name__ == "__main__":
    main()
