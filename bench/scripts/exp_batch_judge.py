"""批量精筛的档位一致率实验（改代码之前的可行性验证）。

问题：把「一张卡一次 LLM 判定」改成「八张卡一次」，会不会让模型判粗？
做法：同一批问题、同一批候选卡，用两种方式各判一遍，比对档位。

判据（事先定好，避免事后解释）：
- 完全一致率 ≥ 90% 且 相邻档（low-mid / mid-high）偏差 ≤ 8%  → 可以批量化
- 过门槛判定（score ≥ 0.35）一致率 ≥ 95%                    → 注入结果基本不变
- 明显低于上述 → 退回逐张，或改成四张一批

用法：python exp_batch_judge.py --n 8 --batch 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter

import bench_lib as B

BATCH_SYSTEM = """你是记忆注入前的精筛判定器。给定用户当前问题和若干候选记忆卡片（带编号），
逐张判断每张卡片对回答问题的价值档位：
- low：不相关，对回答没有帮助（包括已过期的旧状态）
- mid：部分相关，有参考价值但不是回答所必需
- high：高度相关，直接构成或决定回答内容

判断标准与单卡判定完全一致，只是把多张卡放进同一次判断。**各张卡相互独立**：
不要因为别的卡已经占了某个档位就调整这一张，也不要给整批统一打一个档。

只输出一个 json 对象，不要输出任何其他文字。格式：
{"cards": [{"id": 1, "decision": "low 或 mid 或 high", "confidence": 0到1之间的小数}, ...]}
必须覆盖输入里的每一个编号，不多不少。"""

TIER_SCORE = {"high": 0.9, "mid": 0.6, "low": 0.2}
THRESHOLD = 0.35


async def _batch_judge(client, model, query: str, items: list[tuple[int, str]]) -> dict[int, str]:
    """一次判一批；返回 {编号: 档位}。解析失败或缺号 → 缺的按 low（与生产 fail-safe 同向）。"""
    body = "\n".join(f"{i}. {text}" for i, text in items)
    user = f"用户当前问题：{query}\n\n候选记忆卡片：\n{body}"
    r = await client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": BATCH_SYSTEM},
                  {"role": "user", "content": user}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    obj = json.loads(r.choices[0].message.content or "{}")
    got: dict[int, str] = {}
    for item in obj.get("cards") or []:
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        decision = str(item.get("decision", "")).strip().lower()
        if decision in TIER_SCORE:
            got[idx] = decision
    return got


async def main_async(args) -> None:
    B.prepare_env("expbatch", state=args.state)
    B.apply_language_patches()
    from openai import AsyncOpenAI

    from app.core.config import settings
    from app.core.database import AsyncSessionLocal, init_db
    from app.services.long_term_memory import gate, pool, retriever

    client = AsyncOpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL, timeout=60)
    await init_db()
    async with AsyncSessionLocal() as s:
        await pool.build(s)

    sample = B.load_locomo()[args.sample]
    qas = [q for q in B.qa_list(sample) if not q["is_adversarial"]][: args.n]

    sem = asyncio.Semaphore(args.concurrency)
    pairs: list[tuple[str, str]] = []          # (单卡档位, 批量档位)
    per_q = []

    for q in qas:
        async with AsyncSessionLocal() as s:
            res = await retriever.retrieve(q["question"], session=s)
        cards = [(c.ref_id, (c.content or "")[:300]) for c in res.candidates]
        if not cards:
            continue
        # ① 逐张判（生产路径原样调用）
        async def one(text):
            async with sem:
                r = await gate.judge_shortlist(q["question"], text)
                return r.decision if r.decision in TIER_SCORE else "low"
        single = await asyncio.gather(*(one(t) for _, t in cards))
        # ② 批量判
        batched: dict[int, str] = {}
        for i in range(0, len(cards), args.batch):
            chunk = [(i + j + 1, cards[i + j][1]) for j in range(min(args.batch, len(cards) - i))]
            async with sem:
                got = await _batch_judge(client, settings.LLM_MODEL, q["question"], chunk)
            for idx, _ in chunk:
                batched[idx] = got.get(idx, "low")
        batch = [batched[i + 1] for i in range(len(cards))]

        pairs.extend(zip(single, batch))
        agree = sum(1 for a, b in zip(single, batch) if a == b) / len(single)
        pass_s = sum(1 for x in single if TIER_SCORE[x] >= THRESHOLD)
        pass_b = sum(1 for x in batch if TIER_SCORE[x] >= THRESHOLD)
        per_q.append((q["qid"], len(cards), round(agree * 100, 1), pass_s, pass_b))
        print(f"q{q['qid']:3d} 卡 {len(cards):3d}  完全一致 {agree*100:5.1f}%  "
              f"过门槛 单卡 {pass_s:2d} / 批量 {pass_b:2d}", flush=True)

    total = len(pairs)
    exact = sum(1 for a, b in pairs if a == b) / total
    adjacent = sum(1 for a, b in pairs if abs(TIER_SCORE[a] - TIER_SCORE[b]) <= 0.4) / total
    pass_agree = sum(1 for a, b in pairs
                     if (TIER_SCORE[a] >= THRESHOLD) == (TIER_SCORE[b] >= THRESHOLD)) / total
    print(f"\n=== 汇总（{total} 张卡，{len(per_q)} 道题，每批 {args.batch} 张）===")
    print(f"  完全一致      {exact*100:.1f}%")
    print(f"  相邻档以内    {adjacent*100:.1f}%")
    print(f"  过门槛一致    {pass_agree*100:.1f}%")
    print(f"  档位分布 单卡 {dict(Counter(a for a, _ in pairs))}")
    print(f"  档位分布 批量 {dict(Counter(b for _, b in pairs))}")

    path = B.OUT_DIR / f"exp_batch_agree_n{args.n}_b{args.batch}.json"
    path.write_text(json.dumps({"exact": exact, "adjacent": adjacent, "pass_agree": pass_agree,
                                "n_cards": total, "batch": args.batch,
                                "per_q": per_q}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已保存 {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--state", default="s0d", help="用哪套库取候选（默认新库 s0d）")
    ap.add_argument("--n", type=int, default=8, help="跑几道题")
    ap.add_argument("--batch", type=int, default=8, help="每批几张卡")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
