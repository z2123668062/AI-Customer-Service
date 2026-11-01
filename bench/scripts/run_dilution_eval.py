"""稀释实验评测：同一档位下三条对照臂各跑一遍。

三条臂：
- ours ：完整记忆链路（gate → 检索 → 精筛 → 注入 → 生成）
- full ：全上下文（取历史最后 150 条，等于"窗口装满"的容量上限）
- rag  ：朴素 RAG（整段历史 embedding，余弦 top-5，无重排）

全上下文窗口固定 150 条，与有效块等长。噪声追加在有效块之后，所以
噪声越多，full 臂里剩下的有效内容越少，直到完全挤出。

用法：
    python run_dilution_eval.py --noise 0
    python run_dilution_eval.py --noise 25
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time

import bench_lib as B

COND = "dil"
FULL_WINDOW = 150      # 全上下文臂的窗口容量（条）
RAG_TOPK = 5           # 朴素 RAG 的 top-k（约 1000 token，与我们 868 同量级）
EMBED_BATCH = 32


def _load(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


async def _embed_all(texts: list[str]) -> list[list[float]]:
    from app.services.long_term_memory import store
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        chunk = texts[i:i + EMBED_BATCH]
        out.extend(await store.embed_texts(chunk))
    return out


async def _answer(context: str, question: str) -> dict:
    """把一段上下文塞进 prompt 直接问（full / rag 两臂共用）。"""
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
    B.prepare_env(COND, window_size=10)
    B.apply_language_patches()
    import run_eval as RE

    plan = json.loads((B.OUT_DIR / "dilution_plan.json").read_text(encoding="utf-8"))
    msgs = plan["block"] + plan["noise"][: args.noise]
    texts = [m["content"] for m in msgs]
    logical = [{"role": "user", "content": t} for t in texts]
    qas = plan["questions"]

    from app.core.database import AsyncSessionLocal, init_db
    from app.services.long_term_memory import pool, store

    await init_db()
    B.install_spies()
    # 必须关掉提问阶段的写路径：否则每问一题都会往库里写一条消息、触发一次抽取，
    # 后面的题就能看到前面的问答卡，整个库被污染（第一次跑就是栽在这）。
    B.enable_writeoff()
    async with AsyncSessionLocal() as s:
        await store.startup_check(s)
        await pool.build(s)
    store.register_pool_hook(pool.on_store_write)
    async with AsyncSessionLocal() as s:
        cards = await store.list_cards(s, status=None, limit=None)
    print(f"[dil {args.noise}] 历史 {len(texts)} 条（有效块 150 + 噪声 {args.noise}）"
          f"，卡片 {len(cards)} 张，题目 {len(qas)} 道", flush=True)

    vecs = None
    if not args.ours_only:
        t0 = time.perf_counter()
        vecs = await _embed_all(texts)
        print(f"    朴素 RAG 索引：{len(vecs)} 条向量，{time.perf_counter() - t0:.0f}s", flush=True)

    tag = f"n{args.noise}"
    paths = {a: B.OUT_DIR / f"res_dil_{tag}_{a}.jsonl" for a in ("ours", "full", "rag")}
    done = {a: {r["qid"] for r in _load(p) if not r.get("error")} for a, p in paths.items()}

    for i, q in enumerate(qas, 1):
        t0 = time.perf_counter()
        rec = {"qid": q["qid"], "category": q["category"], "question": q["question"],
               "gold": q["gold"], "noise": args.noise, "history": len(texts)}

        if q["qid"] not in done["ours"]:
            try:
                r = await RE.ask_sut(q["question"], logical, 10, tag=f"dil{tag}_{q['qid']}")
                rec["ours"] = {"reply": r["reply"], "tokens": r["tokens"],
                               "judged": r.get("judged"), "elapsed_s": r["elapsed_s"],
                               "injected_ids": r.get("injected_ids")}
                _write(paths["ours"], {**rec, **rec["ours"], "arm": "ours"})
            except Exception as exc:  # noqa: BLE001
                _write(paths["ours"], {**rec, "arm": "ours", "error": f"{type(exc).__name__}: {exc}"})

        if not args.ours_only and q["qid"] not in done["full"]:
            try:
                r = await _answer("\n".join(texts[-FULL_WINDOW:]), q["question"])
                _write(paths["full"], {**rec, **r, "arm": "full"})
            except Exception as exc:  # noqa: BLE001
                _write(paths["full"], {**rec, "arm": "full", "error": f"{type(exc).__name__}: {exc}"})

        if not args.ours_only and q["qid"] not in done["rag"]:
            try:
                qv = (await _embed_all([q["question"]]))[0]
                scores = [_cos(qv, v) for v in vecs]
                top = sorted(range(len(scores)), key=lambda j: -scores[j])[:RAG_TOPK]
                ctx = "\n".join(texts[j] for j in sorted(top))
                r = await _answer(ctx, q["question"])
                r["rag_ids"] = sorted(top)
                _write(paths["rag"], {**rec, **r, "arm": "rag"})
            except Exception as exc:  # noqa: BLE001
                _write(paths["rag"], {**rec, "arm": "rag", "error": f"{type(exc).__name__}: {exc}"})

        if i % 10 == 0 or i == len(qas):
            print(f"    [{i}/{len(qas)}] {time.perf_counter() - t0:.1f}s/题", flush=True)

    print(f"[dil {args.noise}] 完成，结果见 out/res_dil_{tag}_*.jsonl", flush=True)


def _write(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--noise", type=int, default=0)
    ap.add_argument("--ours-only", action="store_true", help="只跑完整系统（跳过全上下文与 RAG）")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
