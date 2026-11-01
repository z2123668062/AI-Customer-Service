"""稀释实验打分：每条臂按严格口径判一遍，输出可直接画图的表。

用法：
    python score_dilution.py               # 扫 out/res_dil_n*_*.jsonl 全部档位
    python score_dilution.py --levels 0 25 # 只看指定档位
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re

import bench_lib as B

PAT = re.compile(r"res_dil_n(\d+)_(\w+)\.jsonl$")


def _load(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def main_async(args) -> None:
    B.prepare_env("dil")
    import judging as J

    files = {}
    for p in sorted(B.OUT_DIR.glob("res_dil_n*_*.jsonl")):
        m = PAT.search(p.name)
        if not m:
            continue
        level, arm = int(m.group(1)), m.group(2)
        if args.levels and level not in args.levels:
            continue
        files.setdefault(level, {})[arm] = p

    sem = asyncio.Semaphore(args.concurrency)
    table = {}
    for level in sorted(files):
        rows = {}
        for arm, path in files[level].items():
            data = [r for r in _load(path) if not r.get("error")]
            cache_path = B.OUT_DIR / f"judged_dil_n{level}_{arm}.jsonl"
            cache = {r["qid"]: r for r in _load(cache_path)}

            async def one(r):
                if r["qid"] in cache:
                    return cache[r["qid"]]
                async with sem:
                    try:
                        v = await J.judge_correct(r["question"], r["gold"], r["reply"] or "")
                        label = v.label
                    except Exception:  # noqa: BLE001
                        return None
                return {"qid": r["qid"], "label": label, "reason": v.reason}

            judged = [x for x in await asyncio.gather(*(one(r) for r in data)) if x]
            with open(cache_path, "w", encoding="utf-8") as f:
                for x in judged:
                    f.write(json.dumps(x, ensure_ascii=False) + "\n")

            by_qid = {x["qid"]: x["label"] for x in judged}
            n = len(by_qid)
            acc = sum(1 for v in by_qid.values() if v == "CORRECT") / n if n else 0
            cat = {}
            for r in data:
                lab = by_qid.get(r["qid"])
                if not lab:
                    continue
                c = r["category"]
                cat.setdefault(c, [0, 0])
                cat[c][1] += 1
                cat[c][0] += 1 if lab == "CORRECT" else 0
            tok = [r.get("tokens", {}).get("total") or 0 for r in data]
            rows[arm] = {
                "n": n,
                "acc": round(acc * 100, 1),
                "by_cat": {k: (round(v[0] / v[1] * 100, 1), v[1]) for k, v in sorted(cat.items())},
                "avg_tokens": round(sum(tok) / len(tok)) if tok else 0,
                "avg_latency": round(sum(r.get("elapsed_s") or 0 for r in data) / len(data), 2) if data else 0,
            }
        table[level] = rows

    out = B.OUT_DIR / "dilution_summary.json"
    out.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'历史':>5s} {'挤出':>5s} | {'全上下文':>8s} {'朴素RAG':>8s} {'本系统':>8s} | "
          f"{'n(full/rag/ours)':>17s} {'token(full/rag/ours)':>21s}")
    for level in sorted(table):
        r = table[level]
        disp = min(level / 150, 1) * 100
        def g(arm):
            return f"{r[arm]['acc']:7.1f}%" if arm in r else "      -"
        ns = "/".join(str(r[a]["n"]) if a in r else "-" for a in ("full", "rag", "ours"))
        tk = " ".join(str(r[a]["avg_tokens"]) if a in r else "-" for a in ("full", "rag", "ours"))
        flag = "" if all(r.get(a, {}).get("n") == 65 for a in ("full", "rag", "ours")) else "  ← 未跑满"
        print(f"{150 + level:5d} {disp:4.0f}% |{g('full')}|{g('rag')}|{g('ours')}| {ns:>17s} {tk:>21s}{flag}")
    print(f"\n已保存 {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", type=int, nargs="*")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
