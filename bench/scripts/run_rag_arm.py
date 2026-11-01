"""朴素 RAG 对照臂：最近 10 条窗口 + 余弦 top-6，与关掉精筛的本系统同预算。

为什么这样配：真实 RAG 产品都有近期窗口，只给 top-k 不给窗口是稻草人。
预算对齐：窗口约 250 token + 6 条消息约 600 token ≈ 850，加上系统提示词与问题
约 1,050，与本系统关精筛版（约 1,250）同量级。

判定与打分复用 score.py（同一 strict judge、同一题集）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time

import bench_lib as B

EMBED_BATCH = 32


def _cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


async def _embed_all(texts: list[str]) -> list[list[float]]:
    from app.services.long_term_memory import store
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(await store.embed_texts(texts[i:i + EMBED_BATCH]))
    return out


async def _answer(context: str, question: str) -> dict:
    from app.core import short_term_memory as hot
    from app.models.schemas import ChatMessage
    from app.services import generation_service

    package = hot.assemble_package(current_input=question,
                                   window=[ChatMessage(role="user", content=context)],
                                   memory_context=None)
    t0 = time.perf_counter()
    gen = await generation_service.generate_reply(package)
    return {
        "reply": gen.reply,
        "elapsed_s": round(time.perf_counter() - t0, 3),
        "tokens": {"prompt": gen.prompt_tokens, "completion": gen.completion_tokens,
                   "total": gen.total_tokens},
        "ctx_chars": len(context),
    }


async def main_async(args) -> None:
    B.prepare_env(args.cond, state="s0d")
    B.apply_language_patches()

    sample = B.load_locomo()[args.sample]
    flat = B.flatten_conversation(sample)
    texts = [m["content"] for m in flat]
    qas = B.qa_list(sample)
    if args.qids:
        keep = set(args.qids)
        qas = [q for q in qas if q["qid"] in keep]

    print(f"[{args.cond}] 消息 {len(texts)} 条，题 {len(qas)} 道，"
          f"窗口 {args.window} + top{args.topk}", flush=True)
    t0 = time.perf_counter()
    vecs = await _embed_all(texts)
    print(f"    索引完成 {len(vecs)} 条，{time.perf_counter() - t0:.0f}s", flush=True)

    out_path = B.OUT_DIR / f"res_{args.cond}.jsonl"
    done = set()
    if out_path.exists() and not args.fresh:
        done = {json.loads(l)["qid"] for l in out_path.read_text(encoding="utf-8").splitlines()
                if l.strip() and not json.loads(l).get("error")}
    if args.fresh:
        out_path.write_text("", encoding="utf-8")

    recent = texts[-args.window:]
    recent_ids = set(range(len(texts) - args.window, len(texts)))

    for i, q in enumerate(qas, 1):
        if q["qid"] in done:
            continue
        rec = {"qid": q["qid"], "category": q["category"], "question": q["question"],
               "gold": B.gold_answer(q), "is_adversarial": q["is_adversarial"]}
        try:
            qv = (await _embed_all([q["question"]]))[0]
            scored = [(j, _cos(qv, v)) for j, v in enumerate(vecs) if j not in recent_ids]
            top = sorted(scored, key=lambda x: -x[1])[:args.topk]
            picked = sorted(j for j, _ in top)                 # 按时间序还原
            ctx = "\n".join(texts[j] for j in picked) + "\n" + "\n".join(recent)
            rec["rag_ids"] = picked
            rec.update(await _answer(ctx, q["question"]))
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if i % 20 == 0 or i == len(qas):
            print(f"    [{i}/{len(qas)}]", flush=True)

    print(f"完成 → {out_path}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--cond", default="s0rag")
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--topk", type=int, default=6)
    ap.add_argument("--qids", type=int, nargs="*")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
