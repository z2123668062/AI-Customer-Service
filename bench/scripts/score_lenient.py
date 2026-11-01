"""双口径补测：对已跑完的条件，用 Mem0 宽松尺子再判一次卷。

用法：python score_lenient.py --cond s0d --cond w40 --cond w20 --cond w10 --common

为什么要它：严格口径（我们的主口径）会拒掉 `Last week.` 这类相对时间答复，
文献里的 66.9 / 61.0 / 72.9 全是宽松口径。两套尺子同时报，才既诚实又能对表。
"""
from __future__ import annotations

import argparse
import asyncio
import json

import bench_lib as B


def _load(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def main_async(args) -> None:
    B.prepare_env("lenient")
    import judging as J

    conds = args.cond
    rows_by_cond = {}
    qid_sets = []
    for cond in conds:
        rows = [r for r in _load(B.OUT_DIR / f"res_{cond}.jsonl") if not r.get("error")]
        rows_by_cond[cond] = rows
        qid_sets.append({r["qid"] for r in rows})
    common = set.intersection(*qid_sets) if qid_sets else set()

    sem = asyncio.Semaphore(args.concurrency)
    out = {}
    for cond in conds:
        judged = {r["qid"]: r for r in _load(B.OUT_DIR / f"judged_{cond}.jsonl")}
        targets = [r for r in rows_by_cond[cond] if r["qid"] in common]
        normal = [r for r in targets if not r.get("is_adversarial")]
        adv = [r for r in targets if r.get("is_adversarial")]

        async def one(row):
            jr = judged.get(row["qid"])
            if not jr or jr.get("judge_label") not in ("CORRECT", "WRONG"):
                return None
            async with sem:
                try:
                    v = await J.judge_lenient(row["question"], row["gold"], row["reply"] or "")
                    return v.label
                except Exception:  # noqa: BLE001
                    return None

        labels = await asyncio.gather(*(one(r) for r in normal))
        ok = [x for x in labels if x]
        strict = [judged[r["qid"]]["judge_label"] for r in normal
                  if judged.get(r["qid"], {}).get("judge_label") in ("CORRECT", "WRONG")]
        strict_acc = sum(1 for x in strict if x == "CORRECT") / len(strict) if strict else 0
        len_acc = sum(1 for x in ok if x == "CORRECT") / len(ok) if ok else 0
        by_cat = {}
        for c in (1, 2, 3, 4):
            pairs = [(r, l) for r, l in zip(normal, labels)
                     if r["category"] == c and l and
                     judged.get(r["qid"], {}).get("judge_label") in ("CORRECT", "WRONG")]
            if pairs:
                by_cat[c] = (round(sum(1 for _, l in pairs if l == "CORRECT") / len(pairs) * 100, 1), len(pairs))
        out[cond] = {
            "n_normal": len(ok),
            "strict_acc": round(strict_acc * 100, 1),
            "lenient_acc": round(len_acc * 100, 1),
            "gap_pp": round((len_acc - strict_acc) * 100, 1),
            "by_cat_lenient": by_cat,
            "avg_tokens": round(sum((r.get("tokens") or {}).get("t") or 0 for r in targets) / max(len(targets), 1)),
            "avg_latency": round(sum(r["elapsed_s"] for r in targets) / max(len(targets), 1), 2),
        }

    path = B.OUT_DIR / "lenient_summary.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"对齐题集 {len(common)} 题（含对抗 {len(adv)}）\n")
    print(f"{'条件':6s} {'严格':>7s} {'宽松':>7s} {'差':>7s}  {'token':>6s} {'延迟':>6s}   分类（宽松）")
    for cond, s in out.items():
        print(f"{cond:6s} {s['strict_acc']:6.1f}% {s['lenient_acc']:6.1f}% {s['gap_pp']:+6.1f}  "
              f"{s['avg_tokens']:6d} {s['avg_latency']:5.1f}s   {s['by_cat_lenient']}")
    print(f"\n已保存 {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cond", action="append", required=True)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
