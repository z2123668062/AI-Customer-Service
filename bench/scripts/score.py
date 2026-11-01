"""评分与报告：读 run_eval 的 JSONL → 判定 → 按 category 汇总。

判定口径见 judging.py（可审阅、可复现）。判定结果缓存到 judged_{cond}.jsonl，
重复跑不会重复付评审费用。

用法：
    python score.py --cond s0 [--cond w10 --cond w20 --cond w40]
    python score.py --cond s0 --rejudge        # 重判（改了口径时用）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from pathlib import Path

import bench_lib as B

CAT_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def _load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


async def judge_all(cond: str, rejudge: bool = False, concurrency: int = 6,
                    qid_filter: set[int] | None = None) -> list[dict]:
    B.prepare_env(cond)
    from app.core.config import settings  # noqa: F401  （触发 settings 加载）

    import judging as J

    rows = _load_jsonl(B.OUT_DIR / f"res_{cond}.jsonl")
    if qid_filter is not None:
        rows = [r for r in rows if r["qid"] in qid_filter]
    cache_path = B.OUT_DIR / f"judged_{cond}.jsonl"
    judged = {r["qid"]: r for r in _load_jsonl(cache_path)}
    if rejudge:
        judged = {}

    sem = asyncio.Semaphore(concurrency)

    async def one(row: dict) -> dict:
        qid = row["qid"]
        if qid in judged:
            return judged[qid]
        async with sem:
            out = dict(row)
            out["f1"], out["precision"], out["recall"] = J.token_f1(
                row.get("reply") or "", row.get("gold") or "")
            out["context_hit"] = J.context_hit(row.get("evidence_texts") or [],
                                               row.get("context_for_hit") or "")
            try:
                if row["is_adversarial"]:
                    v = await J.judge_adversarial(row["question"], row["gold"], row.get("reply") or "")
                    out["adv_label"] = v.label
                else:
                    v = await J.judge_correct(row["question"], row["gold"], row.get("reply") or "")
                    out["adv_label"] = None
                out["judge_label"] = v.label
                out["judge_reason"] = v.reason
                out["judge_origin"] = v.origin
                out["judge_error"] = None
            except Exception as exc:  # noqa: BLE001
                out["judge_label"] = None
                out["adv_label"] = None
                out["judge_reason"] = ""
                out["judge_origin"] = "error"
                out["judge_error"] = f"{type(exc).__name__}: {exc}"
            return out

    results = await asyncio.gather(*(one(r) for r in rows))
    # 缓存取并集（子集评分不丢全量判定结果）
    merged = {**judged, **{r["qid"]: r for r in results}}
    with open(cache_path, "w", encoding="utf-8") as f:
        for qid in sorted(merged):
            f.write(json.dumps(merged[qid], ensure_ascii=False) + "\n")
    return list(results)


def summarize(cond: str, rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("judge_error") and not r.get("error")]
    normal = [r for r in ok if not r["is_adversarial"]]
    adv = [r for r in ok if r["is_adversarial"]]

    def _agg(items: list[dict]) -> dict:
        if not items:
            return {"n": 0}
        return {
            "n": len(items),
            "accuracy": round(sum(1 for r in items if r["judge_label"] == "CORRECT") / len(items), 4),
            "avg_f1": round(statistics.fmean(r["f1"] for r in items), 4),
            "f1_ge_05": round(sum(1 for r in items if r["f1"] >= 0.5) / len(items), 4),
            "context_hit": round(sum(1 for r in items if r["context_hit"]) / len(items), 4),
            "avg_latency_s": round(statistics.fmean(r["elapsed_s"] for r in items), 2),
            "avg_prompt_tokens": round(statistics.fmean(
                (r.get("tokens") or {}).get("prompt") or 0 for r in items), 1),
            "avg_total_tokens": round(statistics.fmean(
                (r.get("tokens") or {}).get("total") or 0 for r in items), 1),
            "injection_rate": round(sum(1 for r in items if r.get("injected_ids")) / len(items), 4),
        }

    by_cat = {}
    for cat, name in CAT_NAMES.items():
        items = [r for r in ok if r["category"] == cat]
        if cat == 5:
            n = len(items)
            by_cat[name] = {
                "n": n,
                "accuracy(refusal)": round(sum(1 for r in items if r["adv_label"] == "REFUSAL") / n, 4) if n else 0,
                "hallucination_rate": round(sum(1 for r in items if r["adv_label"] == "HALLUCINATION") / n, 4) if n else 0,
                "other_rate": round(sum(1 for r in items if r["adv_label"] == "OTHER") / n, 4) if n else 0,
                "avg_latency_s": round(statistics.fmean(r["elapsed_s"] for r in items), 2) if n else 0,
                "injection_rate": round(sum(1 for r in items if r.get("injected_ids")) / n, 4) if n else 0,
            }
        else:
            by_cat[name] = _agg(items)

    from collections import Counter

    triage = Counter(r.get("triage") or "n/a" for r in normal)
    gate = Counter(r.get("gate_origin") or "n/a" for r in normal)
    refusals = sum(1 for r in normal if J_refusal(r.get("reply") or ""))

    return {
        "cond": cond,
        "mode": rows[0]["mode"] if rows else None,
        "window": rows[0]["window"] if rows else None,
        "n_questions": len(rows),
        "n_judged": len(ok),
        "n_error": len(rows) - len(ok),
        "overall_normal(cat1-4)": _agg(normal),
        "by_category": by_cat,
        "adversarial(cat5)": {
            "n": len(adv),
            "refusal_rate": round(sum(1 for r in adv if r["adv_label"] == "REFUSAL") / len(adv), 4) if adv else 0,
            "hallucination_rate": round(sum(1 for r in adv if r["adv_label"] == "HALLUCINATION") / len(adv), 4) if adv else 0,
            "other_rate": round(sum(1 for r in adv if r["adv_label"] == "OTHER") / len(adv), 4) if adv else 0,
        },
        "diagnostics": {
            "triage_dist": dict(triage),
            "gate_origin_dist": dict(gate),
            "abstain_rate_on_normal": round(refusals / len(normal), 4) if normal else 0,
        },
    }


def J_refusal(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in (
        "not mentioned", "no mention", "not specified", "not stated", "not discussed",
        "i don't know", "i do not know", "cannot be determined", "no information",
        "not enough information", "does not say", "doesn't say", "n/a",
    ))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cond", action="append", required=True)
    ap.add_argument("--rejudge", action="store_true")
    ap.add_argument("--common", action="store_true", help="各条件对齐到同一题集（取交集）")
    args = ap.parse_args()

    all_summary = []
    qid_filter: set[int] | None = None
    if args.common:
        avail = []
        for cond in args.cond:
            rows = _load_jsonl(B.OUT_DIR / f"res_{cond}.jsonl")
            avail.append({r["qid"] for r in rows if not r.get("error")})
        qid_filter = set.intersection(*avail) if avail else None
        print(f"对齐题集：{len(qid_filter or [])} 题（各条件取交集）")

    for cond in args.cond:
        rows = asyncio.run(judge_all(cond, rejudge=args.rejudge, qid_filter=qid_filter))
        if not rows:
            print(f"[{cond}] 无结果，跳过")
            continue
        s = summarize(cond, rows)
        s["qid_filter"] = len(qid_filter) if qid_filter else "all"
        all_summary.append(s)
        (B.OUT_DIR / f"summary_{cond}.json").write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n===== {cond}（mode={s['mode']} window={s['window']}）=====")
        print(json.dumps(s["overall_normal(cat1-4)"], ensure_ascii=False))
        for name, v in s["by_category"].items():
            print(f"  {name:12s} {json.dumps(v, ensure_ascii=False)}")
        print(f"  adversarial  {json.dumps(s['adversarial(cat5)'], ensure_ascii=False)}")

    (B.OUT_DIR / "summary_all.json").write_text(
        json.dumps(all_summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
