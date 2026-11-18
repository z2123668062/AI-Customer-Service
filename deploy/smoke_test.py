"""部署冒烟测试：中文 SSE 对话 + 记忆接口。

用法：
    python deploy/smoke_test.py [base_url]
    SMOKE_AUTH=admin:密码 python deploy/smoke_test.py [base_url]

默认 base_url = http://localhost:8080（本地 compose 的 nginx）。
nginx 已开 Basic Auth：不带 SMOKE_AUTH 时会拿到 401，脚本会提示。

说明：Windows 下用 Git Bash 的 curl -d 直接传中文会被编码成非 UTF-8，
后端返回 400；用本脚本（Python httpx，UTF-8）可复现真实浏览器行为。
"""
import json
import os
import sys
import uuid

import httpx

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8080"

# Basic Auth：SMOKE_AUTH="用户名:密码"。不设则不带凭证（只用于验证 401 拦截）。
_raw_auth = os.environ.get("SMOKE_AUTH", "")
AUTH = tuple(_raw_auth.split(":", 1)) if ":" in _raw_auth else None


def chat(session_id: str, message: str) -> tuple[str, dict | None, list[str]]:
    events: list[dict] = []
    with httpx.stream(
        "POST",
        f"{BASE}/api/v1/chat/",
        json={"session_id": session_id, "message": message},
        auth=AUTH,
        timeout=60,
    ) as resp:
        print("chat HTTP:", resp.status_code)
        if resp.status_code == 401:
            print("401：nginx 鉴权拦住了。设 SMOKE_AUTH=用户名:密码 后重试。")
            return "", None, []
        if resp.status_code != 200:
            print(resp.read().decode("utf-8", "replace")[:500])
            return "", None, []
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    reply = "".join(e["content"] for e in events if e["event"] == "message")
    meta = next((e["content"] for e in events if e["event"] == "meta"), None)
    kinds = sorted({e["event"] for e in events})
    print("reply:", reply)
    print("meta:", {"status": meta.get("status"), "count": meta.get("count")} if meta else None)
    print("events:", kinds)
    return reply, meta, kinds


def main() -> int:
    session_id = f"smoke_{uuid.uuid4().hex[:8]}"
    reply, meta, kinds = chat(session_id, "你好，我叫小羊，请记住")

    resp = httpx.get(f"{BASE}/api/v1/memory", auth=AUTH, timeout=10)
    print("memory HTTP:", resp.status_code)
    if resp.status_code == 200:
        data = resp.json()
        print("memory items:", len(data) if isinstance(data, list) else list(data)[:5])

    ok = bool(reply) and "done" in kinds and "status" in kinds
    print("SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
