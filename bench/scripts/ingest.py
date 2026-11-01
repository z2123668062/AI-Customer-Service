"""灌库：把 LoCoMo 一段对话按顺序写进毕设系统（不改记忆逻辑）。

链路说明（与线上唯一差别）：
- 线上：HTTP → chat_pipeline → 生成回复 → 归档 → 写路径触发；
- 这里：直接归档（history_service.save_record_to_db，与线上同一函数）→ 写路径触发
  （consolidator.note_activity，与线上同一函数）→ 等后台抽取收敛。
不生成助手回复的原因：LoCoMo 是两个人（Caroline / Melanie）的真实对话，让毕设助手
逐条应答等于往数据里灌 419 条虚构发言；而抽取器本身就规定「不抽助手说的话」，
所以只灌用户侧原文与线上记忆内容等价。

用法：
    python ingest.py --sample 0 --cond s0
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime

import bench_lib as B

_DATE_FORMAT = "%I:%M %p on %d %B, %Y"   # LoCoMo: "1:56 pm on 8 May, 2023"


def parse_session_date(text: str | None) -> datetime | None:
    """LoCoMo 会话日期 → datetime（L3：作为 messages.created_at 灌库）。

    解析失败返回 None（退化为入库时刻）——宁可丢时间，不要让整批灌库失败。
    """
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), _DATE_FORMAT)
    except ValueError:
        return None


async def run(sample_idx: int, cond: str, *, limit: int | None = None,
              window_size: int = 10) -> dict:
    B.prepare_env(cond, window_size=window_size)

    from app.core.database import AsyncSessionLocal, init_db
    from app.services.history_service import save_record_to_db
    from app.services.long_term_memory import consolidator, store

    B.apply_language_patches()

    sample = B.load_locomo()[sample_idx]
    msgs = B.flatten_conversation(sample)
    if limit:
        msgs = msgs[:limit]

    await init_db()
    async with AsyncSessionLocal() as session:
        await store.startup_check(session)

    session_id = f"locomo_s{sample_idx}"
    t0 = time.perf_counter()
    triggers: dict[str, int] = {}
    for i, m in enumerate(msgs, 1):
        mid = await save_record_to_db(session_id, "user", m["content"],
                                      created_at=parse_session_date(m.get("session_date")))
        if mid is None:
            raise RuntimeError(f"归档失败：第 {i} 条")
        reason = await consolidator.note_activity(session_id=session_id)
        if reason:
            triggers[reason] = triggers.get(reason, 0) + 1
        await consolidator.wait_idle(timeout=300)
        if i % 25 == 0 or i == len(msgs):
            print(f"  [{i}/{len(msgs)}] {time.perf_counter() - t0:.0f}s "
                  f"触发={triggers}", flush=True)

    # 收尾：不足 8 条待抽的尾巴
    rounds = await consolidator.backfill(drain=True)
    await consolidator.wait_idle(timeout=300)

    async with AsyncSessionLocal() as session:
        cards = await store.list_cards(session, status=None, limit=None)
        max_id, _ = await store.get_global_message_stats(session)
    from collections import Counter
    kinds = Counter((c.kind, c.subtype) for c in cards)
    lag = await consolidator.lag_report()

    summary = {
        "cond": cond,
        "sample": sample_idx,
        "messages": len(msgs),
        "card_total": len(cards),
        "cards_active": sum(1 for c in cards if c.status == "active"),
        "cards_by_kind": {f"{k}/{s}": v for (k, s), v in kinds.items()},
        "backfill_rounds": rounds,
        "triggers": triggers,
        "max_message_id": max_id,
        "watermark": lag["watermark"],
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }
    print(f"\n灌库完成：{summary}", flush=True)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--cond", default=None, help="状态目录名（默认 s{样本}）")
    ap.add_argument("--limit", type=int, default=None, help="只灌前 N 条（单题验证用）")
    ap.add_argument("--window", type=int, default=10)
    args = ap.parse_args()
    cond = args.cond or f"s{args.sample}"
    res = asyncio.run(run(args.sample, cond, limit=args.limit, window_size=args.window))
    B.OUT_DIR.mkdir(parents=True, exist_ok=True)
    (B.OUT_DIR / f"ingest_{cond}.json").write_text(
        __import__("json").dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
