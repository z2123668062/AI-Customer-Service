"""增量灌库：把「有效块 + 前 N 条噪声」灌进同一个库，可反复调用逐档加深。

为什么要增量：8 个档位如果各自从零灌一次，等于灌 8 遍有效块（多花两小时）。
增量方式只灌一遍，每档只追加新噪声，总成本 = 600 条消息。

用法：
    python ingest_dilution.py --noise 0      # 只灌有效块 150 条
    python ingest_dilution.py --noise 25     # 再追加 25 条噪声
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import bench_lib as B

COND = "dil"


async def main_async(args) -> None:
    B.prepare_env(COND, window_size=10)
    B.apply_language_patches()

    plan = json.loads((B.OUT_DIR / "dilution_plan.json").read_text(encoding="utf-8"))
    msgs = plan["block"] + plan["noise"][: args.noise]
    target = len(msgs)

    from app.core.database import AsyncSessionLocal, init_db
    from app.services.history_service import save_record_to_db
    from app.services.long_term_memory import consolidator, evictor, pool, store

    await init_db()
    async with AsyncSessionLocal() as s:
        await store.startup_check(s)

    # 已有多少条（增量续灌）
    async with AsyncSessionLocal() as s:
        have = (await store.list_messages(s, after_id=0, limit=10_000))
    start = len(have)
    if start >= target:
        print(f"库里已有 {start} 条，达到目标 {target}，无需追加")
    else:
        print(f"库内 {start} 条 → 目标 {target} 条（追加 {target - start} 条）", flush=True)
        t0 = time.perf_counter()
        triggers: dict[str, int] = {}
        for i in range(start, target):
            m = msgs[i]
            mid = await save_record_to_db("locomo_dil", "user", m["content"],
                                          created_at=_parse(m["created_at"]))
            if mid is None:
                raise RuntimeError(f"归档失败：第 {i} 条")
            reason = await consolidator.note_activity(session_id="locomo_dil")
            if reason:
                triggers[reason] = triggers.get(reason, 0) + 1
            await consolidator.wait_idle(timeout=300)
            if (i + 1) % 25 == 0 or i + 1 == target:
                print(f"  [{i + 1}/{target}] {time.perf_counter() - t0:.0f}s 触发={triggers}", flush=True)
        # 收尾：把余下不足一批的尾巴抽掉（与 ingest.py 同一收口）
        await consolidator.backfill(drain=True)
        await consolidator.wait_idle(timeout=300)
        print(f"灌库完成：{time.perf_counter() - t0:.0f}s", flush=True)

    # 每次灌完都做一次快表重建与体检，供评测用
    async with AsyncSessionLocal() as s:
        await store.startup_check(s)
        await pool.build(s)
    async with AsyncSessionLocal() as s:
        cards = await store.list_cards(s, status=None, limit=None)
        active = [c for c in cards if c.status == "active"]
        nodes = [c for c in cards if c.canonical_name]
        with_time = [c for c in cards if c.event_time]
        edges = await store.list_edges(s) if hasattr(store, "list_edges") else []
        print(f"体检：卡 {len(cards)}（active {len(active)}，节点 {len(nodes)}，"
              f"有事件时间 {len(with_time)}）边 {len(edges)}", flush=True)


def _parse(text: str):
    from datetime import datetime
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--noise", type=int, default=0)
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
