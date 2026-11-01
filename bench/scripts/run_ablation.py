"""机制消融：把「按相关性选卡」换成「按时间新旧选卡」，其余一切不变。

要回答的问题：稀释实验里本系统那条线之所以是平的，究竟是不是因为
候选是按相关性挑出来的？

做法：不跑检索、不跑精筛，直接从库里取最近 8 张卡注入。这就是一个
"只记得最近发生什么"的策略，对应全上下文那条线的行为方式，但用的是卡片。

层次怎么重建：灌库是增量做的，单个库里已经有全部 600 条消息的卡。
卡片带来源字段（source_message_ids），所以可以按"这张卡是否来自前 N 条消息"
筛出某一档当时实际拥有的那批卡——等价于那一档的库。

用法：
    python run_ablation.py --levels 0 150 --topk 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import bench_lib as B

COND = "dil"
BLOCK = 150


def _load(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _from_prefix(card, upto: int) -> bool:
    ids = card.source_message_ids or []
    return bool(ids) and max(ids) <= upto


async def main_async(args) -> None:
    B.prepare_env(COND, window_size=10)
    B.apply_language_patches()

    from app.core.database import AsyncSessionLocal, init_db
    from app.services.long_term_memory import injector, pool, retriever, store
    from run_dilution_eval import _answer

    plan = json.loads((B.OUT_DIR / "dilution_plan.json").read_text(encoding="utf-8"))
    qas = plan["questions"]

    await init_db()
    async with AsyncSessionLocal() as s:
        await store.startup_check(s)
        await pool.build(s)
        cards = [c for c in await store.list_cards(s, status=None, limit=None) if c.status == "active"]
    print(f"[消融·按时间选卡] 库内 active 卡 {len(cards)} 张", flush=True)

    for noise in args.levels:
        upto = BLOCK + noise
        pool_cards = [c for c in cards if _from_prefix(c, upto)]
        newest = sorted(pool_cards, key=lambda c: c.id, reverse=True)[: args.topk]
        if not newest:
            print(f"[档 {noise}] 没有可用的卡，跳过", flush=True)
            continue
        cands = [retriever._candidate_from_card(c, {"recency"}, None, None) for c in newest]
        ctx = injector.render(cands)
        print(f"\n[档 {noise}] 该档卡片 {len(pool_cards)} 张，取最近 {len(newest)} 张，"
              f"注入段 {len(ctx)} 字", flush=True)

        out_path = B.OUT_DIR / f"res_abl_rec{args.topk}_n{noise}.jsonl"
        done = {r["qid"] for r in _load(out_path) if not r.get("error")}
        for i, q in enumerate(qas, 1):
            if q["qid"] in done:
                continue
            rec = {"qid": q["qid"], "category": q["category"], "question": q["question"],
                   "gold": q["gold"], "noise": noise, "arm": f"recency_top{args.topk}"}
            try:
                rec.update(await _answer(ctx, q["question"]))
            except Exception as exc:  # noqa: BLE001
                rec["error"] = f"{type(exc).__name__}: {exc}"
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if i % 15 == 0 or i == len(qas):
                print(f"    [{i}/{len(qas)}]", flush=True)
        print(f"    完成 → {out_path}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", type=int, nargs="+", default=[0, 150],
                    help="在哪些噪声档位下测（0 和 150 分别是 0%% 和 100%% 挤出）")
    ap.add_argument("--topk", type=int, default=8)
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
