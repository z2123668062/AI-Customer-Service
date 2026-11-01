"""单题链路探针：把 gold / 模型回答 / 注入的记忆 / gate 判定并排打印，供人工判断。

用法：
    python probe.py --sample 0 --cond s0 --qid 4 6 12 [--window 10]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import bench_lib as B


async def ask(question: str, msgs: list[dict], *, tag: str, window: int,
              use_memory: bool = True) -> dict:
    """一次提问：use_memory=True 走完整记忆链路（chat_pipeline）；False 为纯窗口对照。"""
    from app.core import short_term_memory as hot
    from app.models.schemas import ChatMessage
    from app.services import generation_service

    sid = f"probe_{tag}_{int(time.time() * 1000)}"
    await hot.redis_client.delete(hot._key(sid))
    for m in (msgs[-window:] if window > 0 else []):
        await hot.redis_client.rpush(hot._key(sid), ChatMessage(role=m["role"],
                                                               content=m["content"]).model_dump_json())

    B.reset_capture()
    t0 = time.perf_counter()
    if use_memory:
        from app.services import chat_pipeline
        outcome = await chat_pipeline.handle_message(sid, question)
        reply = outcome.reply
    else:
        package = hot.assemble_package(current_input=question,
                                       window=[ChatMessage(role=m["role"], content=m["content"])
                                               for m in (msgs[-window:] if window > 0 else [])],
                                       memory_context=None)
        gen = await generation_service.generate_reply(package)
        reply = gen.reply
    dt = time.perf_counter() - t0
    await hot.redis_client.delete(hot._key(sid))

    cap = B.capture()
    return {
        "reply": reply,
        "elapsed_s": round(dt, 2),
        "memory_text": cap.memory_text,
        "injected": cap.injector_trace.get("injected"),
        "triage": (cap.retrieval_trace or {}).get("triage"),
        "gate": (cap.retrieval_trace or {}).get("gate"),
        "candidates": (cap.retrieval_trace or {}).get("merged_count"),
        "judged": (cap.rerank_trace or {}).get("judged"),
        "tiers": (cap.rerank_trace or {}).get("tiers"),
        "tokens": {"p": cap.prompt_tokens, "c": cap.completion_tokens, "t": cap.total_tokens},
    }


async def main_async(args) -> None:
    B.prepare_env(args.cond, window_size=args.window, state=getattr(args, "state", None))
    from app.core.database import AsyncSessionLocal, init_db
    from app.services.long_term_memory import pool, store

    B.apply_language_patches()
    B.install_spies()
    B.enable_writeoff()

    await init_db()
    async with AsyncSessionLocal() as session:
        await pool.build(session)      # 快表预热（启动钩子之一）
        cards = await store.list_cards(session, status=None, limit=None)

    sample = B.load_locomo()[args.sample]
    flat = B.flatten_conversation(sample)
    msgs = [{"role": "user", "content": m["content"]} for m in flat]
    print(f"库内卡片 {len(cards)} 张；对话 {len(msgs)} 条；窗口 {args.window}\n")

    rows = []
    for qid in args.qid:
        item = B.qa_list(sample)[qid]
        gold = B.gold_answer(item)
        ev = B.evidence_texts(sample, item.get("evidence", []))
        res = await ask(item["question"], msgs, tag=f"q{qid}", window=args.window)
        print("=" * 100)
        print(f"[Q{qid}] cat={item.get('category')}  {item['question']}")
        print(f"  GOLD   : {gold or '(adversarial: ' + B.gold_answer({**item, 'answer': item.get('adversarial_answer','')}) + ')'}")
        print(f"  MODEL  : {res['reply']!r}")
        print(f"  triage={res['triage']} gate={res['gate'] and res['gate'].get('decision')}"
              f" merged={res['candidates']} judged={res['judged']} tiers={res['tiers']}")
        print(f"  tokens={res['tokens']}  latency={res['elapsed_s']}s")
        print(f"  EVIDENCE(原文): {ev}")
        print(f"  MEMORY_TEXT:\n{res['memory_text']}")
        rows.append({"qid": qid, "item": item, "gold": gold, "result": res, "evidence": ev})

    out = B.OUT_DIR / f"probe_{args.cond}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n已保存 {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--cond", default="s0")
    ap.add_argument("--state", default=None, help="复用哪套库/向量集合（默认同 cond）")
    ap.add_argument("--qid", type=int, nargs="+", required=True)
    ap.add_argument("--window", type=int, default=10)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
