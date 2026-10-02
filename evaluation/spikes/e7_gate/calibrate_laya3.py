# -*- coding: utf-8 -*-
r"""laya 调用规格校准·第三轮（E7 · 一次性受控实验，收敛轮）。

前两轮结论：
  - page_fault：短句/陈述句/长句在 v2 全量 24 条上均为 0.46~0.54，两组分布重叠；
  - supersede ：4 类变体 0~2/5，模型对"属性覆盖"语义不稳定。

本轮最后两个假设（每假设只做一次验证，不穷举）：
  A. page_fault：正向"需要记忆"表述或绑定语用（"回答它"）是障碍；
     改用负向独立可答表述（W1/W2）与 choice 二选一（W3）。
  B. supersede：单次三选一负担过重；拆成两个 noul（先判相关、再判取代），
     Q1 低→unrelated；Q1 高 & Q2 低→coexist；Q1 高 & Q2 高→supersede。

运行（项目 venv，cwd=AI-Customer-Service）：
    $env:HF_ENDPOINT="https://hf-mirror.com"
    & "E:\毕设_记忆系统\.venv\Scripts\python.exe" -X utf8 `
        -m evaluation.spikes.e7_gate.calibrate_laya3
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

SPIKE_DIR = Path(__file__).resolve().parent
V1_DATASET = SPIKE_DIR / "dataset.jsonl"
V2_DATASET = SPIKE_DIR / "dataset_v2.jsonl"

PF_VARIANTS = {
    "W1_负向独立": {
        "type": "noul",
        "instructions": "该消息不需要任何历史信息，只看这一条消息就能正确回答它。",
        "criteria": {"true": "可以独立回答", "false": "需要历史信息"},
    },
    "W2_负向完整": {
        "type": "noul",
        "instructions": "这是一条独立完整的消息，回答它不需要回忆用户过去说过的话、经历或个人信息。",
        "criteria": {"true": "可以独立回答", "false": "需要历史信息"},
    },
    "W3_choice二选一": {
        "type": "choice",
        "instructions": "回答这条消息，需要查到用户的历史记忆吗？",
        "criteria": {
            "need_memory": "需要：答案依赖用户过往对话中的信息",
            "no_memory": "不需要：只看当前消息就能正确回答",
        },
    },
}
# W1/W2 负向：true=可独立回答 → 需要记忆 = noul < 0.5

SUP_Q1 = {"type": "noul",
          "instructions": "新信息与旧卡片讨论的是同一个话题或同一个对象（同一个人、地点、喜好或事物）。"}
SUP_Q2 = {"type": "noul",
          "instructions": "新信息更新了旧卡片的同一属性，旧卡片的内容已经过时。"}


def load(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    v1 = {s["id"]: s for s in load(V1_DATASET)}
    v2 = load(V2_DATASET)

    from laya import Router

    router = Router()
    t0 = time.perf_counter()
    router.predict("你好。", {"warm": {"type": "noul", "instructions": "这是一句问候。"}})
    print(f"预热 {1000 * (time.perf_counter() - t0):.0f}ms\n")

    # ---------- A. page_fault 三变体（24 条） ----------
    print("=" * 78)
    print("A. page_fault：负向表述与 choice 形态（dataset_v2 全量 24 条）")
    print("=" * 78)
    pf_samples = [s for s in v2 if s["task"] == "page_fault"]
    for vname, q in PF_VARIANTS.items():
        rows = []
        for s in pf_samples:
            state = f"【当前用户消息】\n{s['input']['query']}"
            r = router.predict(state, {"pf": q})
            a = r["answers"]["pf"]
            if "noul" in a:  # 负向：低 noul = 需要记忆
                dec = "yes" if a["noul"] < 0.5 else "no"
                score = a["noul"]
            else:
                dec = "yes" if a["choice"] == "need_memory" else "no"
                score = (a.get("probabilities") or {}).get("need_memory", 0.0)
            rows.append({"id": s["id"], "gold": s["gold"], "dec": dec, "ok": dec == s["gold"],
                         "score": score, "trap": s.get("trap", ""), "query": s["input"]["query"]})
        acc = sum(r["ok"] for r in rows) / len(rows)
        ys = [r["score"] for r in rows if r["gold"] == "yes"]
        ns = [r["score"] for r in rows if r["gold"] == "no"]
        print(f"\n[{vname}] acc={acc:.3f}")
        print(f"  分数分布（越高越'需要记忆'）：yes组 中位数={statistics.median(ys):.3f} | "
              f"no组 中位数={statistics.median(ns):.3f}")
        wrong = [r for r in rows if not r["ok"]]
        if wrong:
            print(f"  误判 {len(wrong)} 条：")
            for r in wrong:
                print(f"    {r['id']} [{r.get('trap') or '-'}] gold={r['gold']} → {r['dec']} "
                      f"(score={r['score']:.3f}) | {r['query']}")
        else:
            print("  全部正确")

    # ---------- B. supersede 双问法（5 条） ----------
    print()
    print("=" * 78)
    print("B. supersede：双 noul 拆解（5 样例）")
    print("=" * 78)
    hits = 0
    for sid in ["sup_01", "sup_02", "sup_05", "sup_11", "sup_14"]:
        s = v1[sid]
        state = f"【已有旧记忆卡片】\n{s['input']['old_card']}\n【新出现的信息】\n{s['input']['new_info']}"
        r = router.predict(state, {"q1": SUP_Q1, "q2": SUP_Q2})
        p1 = r["answers"]["q1"]["noul"]
        p2 = r["answers"]["q2"]["noul"]
        if p1 < 0.5:
            dec = "unrelated"
        elif p2 >= 0.5:
            dec = "supersede"
        else:
            dec = "coexist"
        ok = dec == s["gold"]
        hits += ok
        print(f"  {sid} gold={s['gold']:9s} → {dec:9s} {'✓' if ok else '✗'}  "
              f"(p_rel={p1:.3f}, p_sup={p2:.3f}) | {s['input']['old_card']} → {s['input']['new_info']}")
    print(f"\n双问法命中：{hits}/5")
    return 0


if __name__ == "__main__":
    sys.exit(main())
