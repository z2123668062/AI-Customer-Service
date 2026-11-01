"""上限探测与漏点漏斗：判断分数到底丢在写路径、读路径，还是生成侧。

两个测量：
1. **oracle 上限**：把标准证据原文（LoCoMo 的 evidence 对应的发言）直接塞给模型，
   同一个 system prompt、同一个模型、同一个判定。答对了 → 证据够用，问题在检索；
   答不对 → 问题在生成侧（模型/prompt），再好的检索也救不回来。
2. **卡片漏斗**（离线，零成本）：标准答案的实词，在「全部卡片」里有没有、在「注入段」里有没有。
   三级漏斗 = 原文里有 → 抽成卡了 → 注入进去了 → 答对了。

用法：python oracle_test.py --sample 0
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys

import bench_lib as B


async def main_async(args) -> None:
    B.prepare_env("oracle", state="s0")
    B.apply_language_patches()
    from app.core import short_term_memory as hot
    from app.models.schemas import ChatMessage
    from app.services import generation_service

    import judging as J

    sample = B.load_locomo()[args.sample]
    qas = [q for q in B.qa_list(sample) if not q["is_adversarial"]]
    rows = [json.loads(line) for line in
            (B.OUT_DIR / f"res_{args.base}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    judged = [json.loads(line) for line in
              (B.OUT_DIR / f"judged_{args.base}.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    jmap = {r["qid"]: r for r in judged}
    base = {}
    for r in rows:                              # 原始记录 + 判定结果合并
        merged = dict(r)
        merged.update({k: v for k, v in jmap.get(r["qid"], {}).items() if k in ("judge_label", "reply")})
        base[r["qid"]] = merged
    qas = [q for q in qas if q["qid"] in base]

    # 全部卡片正文（漏斗第二级用）
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.domain import Card
    async with AsyncSessionLocal() as s:
        cards = list((await s.execute(select(Card))).scalars().all())
    card_blob = "\n".join(c.content or "" for c in cards)
    card_words = set(J._content_words(card_blob))

    sem = asyncio.Semaphore(args.concurrency)
    results = []

    flat = B.flatten_conversation(sample)
    session_of = {m["dia_id"]: m["session"] for m in flat if m["dia_id"]}
    by_session: dict[str, list[str]] = {}
    for m in flat:
        by_session.setdefault(m["session"], []).append(m["content"])

    async def one(q: dict) -> dict:
        async with sem:
            ev = B.evidence_texts(sample, q.get("evidence", []))
            gold = B.gold_answer(q)
            if args.oracle_mode == "session":
                # 检索完美的近似：把证据所在的整个 session（可跨多个）原样给出
                sess = []
                for e in q.get("evidence", []):
                    s = session_of.get(e)
                    if s and s not in sess:
                        sess.append(s)
                texts = []
                for s in sess:
                    if args.with_date:      # 带上会话日期（temporal 类的公平上限）
                        texts.append(f"[Session date: {sample['conversation'].get(f'{s}_date_time')}]")
                    texts.extend(by_session.get(s, []))
            else:
                texts = ev
            win = [ChatMessage(role="user", content=t) for t in texts]
            pkg = hot.assemble_package(current_input=q["question"], window=win,
                                       memory_context=None)
            gen = await generation_service.generate_reply(pkg)
            pred = gen.reply
            try:
                v = await J.judge_correct(q["question"], gold, pred)
                label = v.label
            except Exception as exc:  # noqa: BLE001
                label = f"ERR:{exc}"
            gw = set(J._content_words(gold))
            base_row = base[q["qid"]]
            return {
                "qid": q["qid"], "category": q.get("category"),
                "question": q["question"], "gold": gold,
                "oracle_pred": pred, "oracle_label": label,
                "evidence": ev,
                "gold_in_cards": (len(gw & card_words) / len(gw)) if gw else None,
                "gold_in_injected": (len(gw & set(J._content_words(base_row.get("injected_text") or "")))
                                     / len(gw)) if gw else None,
                "system_label": base_row.get("judge_label"),
                "system_pred": base_row.get("reply"),
            }

    results = await asyncio.gather(*(one(q) for q in qas))

    ok = [r for r in results if r["oracle_label"] in ("CORRECT", "WRONG")]
    oracle_acc = sum(1 for r in ok if r["oracle_label"] == "CORRECT") / len(ok)
    sys_ok = [r for r in results if r["system_label"] in ("CORRECT", "WRONG")]
    sys_acc = sum(1 for r in sys_ok if r["system_label"] == "CORRECT") / len(sys_ok)

    print(f"\n=== oracle 上限（证据原文直接给模型）n={len(ok)} ===")
    print(f"  正确率 {oracle_acc*100:.1f}%   现行系统 {sys_acc*100:.1f}%")
    by_cat = {}
    for c in (1, 2, 3, 4):
        items = [r for r in ok if r["category"] == c]
        if items:
            by_cat[c] = (round(sum(1 for r in items if r["oracle_label"] == "CORRECT") / len(items) * 100, 1),
                         len(items))
    print(f"  分类 oracle: {by_cat}")

    print("\n=== 卡片漏斗（标准答案实词覆盖率）===")
    def _bucket(v):
        if v is None:
            return "n/a"
        if v >= 0.99:
            return "全在"
        if v >= 0.5:
            return "过半"
        if v > 0:
            return "少量"
        return "没有"
    from collections import Counter
    print("  卡片库 :", dict(Counter(_bucket(r["gold_in_cards"]) for r in results)))
    print("  注入段 :", dict(Counter(_bucket(r["gold_in_injected"]) for r in results)))

    print("\n=== 漏点分类（现行系统的 44 道正常题）===")
    def cls(r):
        had = r["gold_in_injected"] or 0
        if r["system_label"] == "CORRECT":
            return "答对"
        if had >= 0.5:
            return "注入了线索但没答对（生成侧）"
        if (r["gold_in_cards"] or 0) >= 0.5:
            return "抽成卡了但没注入（读路径）"
        return "卡片里就没有（写路径）"
    print(" ", dict(Counter(cls(r) for r in results)))

    out = B.OUT_DIR / f"oracle_{args.base}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存 {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--base", default="s0", help="用哪份 res_*.jsonl 作现行系统对照")
    ap.add_argument("--oracle-mode", default="session", choices=("session", "evidence"),
                    help="session=给整个会话（检索完美近似）；evidence=只给证据句（证据句上限）")
    ap.add_argument("--with-date", action="store_true",
                    help="session 模式下给每段加 [Session date: ...]（temporal 的公平上限）")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
