# -*- coding: utf-8 -*-
r"""laya 调用规格校准·第二轮（E7 · 一次性受控实验）。

第一轮结论（calibrate_laya.py）：
  - page_fault：V2 简洁疑问句 4/4、V4 陈述句+criteria 4/4；长指令 V1 污染判定。
  - supersede ：简化 criteria 仅小改善，模型仍强偏 coexist。

本轮两个问题：
  A. page_fault 的 V2 vs V4 谁更稳健？→ 用 dataset_v2 全部 24 条对决（统计意义）。
  B. supersede 的失败根因？→ 5 样例 × 4 变体：
     C2 简化(对照) / C3 简state / C4 英文criteria / C5 直白criteria。

运行（项目 venv，cwd=AI-Customer-Service）：
    $env:HF_ENDPOINT="https://hf-mirror.com"
    & "E:\毕设_记忆系统\.venv\Scripts\python.exe" -X utf8 `
        -m evaluation.spikes.e7_gate.calibrate_laya2
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

_PF_STMT = "该消息必须依赖用户过往对话中的个人信息、经历、偏好或约定才能正确回应。"
PF_VARIANTS = {
    "V2_简洁疑问句": {"type": "noul", "instructions": "回答该问题是否需要调取用户的历史记忆？"},
    "V4_陈述句+criteria": {
        "type": "noul",
        "instructions": _PF_STMT,
        "criteria": {"true": "需要查历史记忆", "false": "不需要历史记忆"},
    },
}

_SUP_SIMPLE = {
    "unrelated": "主题不同，互不影响",
    "coexist": "主题相关，但两者可以同时成立（补充、新增或新事件）",
    "supersede": "同一属性被改写，旧内容过期失效",
}
_SUP_DIRECT = {
    "unrelated": "两件事主题不同，没有关联。",
    "coexist": "两件事相关，但都是事实，可以同时成立。",
    "supersede": "新信息更新了旧信息的同一属性（住址、工作、偏好、号码这类），旧内容已经过时。",
}
_SUP_EN = {
    "unrelated": "Different topics, no interaction.",
    "coexist": "Related, but both statements can hold at the same time.",
    "supersede": "Updates the same attribute (address, job, preference, phone number), so the old card is obsolete.",
}
_SUP_INSTR = "新出现的信息与已有旧记忆卡片属于哪种关系？"


def state_sup_orig(s: dict) -> str:
    return f"【已有旧记忆卡片】\n{s['input']['old_card']}\n【新出现的信息】\n{s['input']['new_info']}"


def state_sup_simple(s: dict) -> str:
    return f"旧记忆：{s['input']['old_card']}\n新信息：{s['input']['new_info']}"


SUP_VARIANTS = {
    "C2_简化(对照)": (state_sup_orig, {"type": "choice", "instructions": _SUP_INSTR, "criteria": _SUP_SIMPLE}),
    "C3_简state+简化": (state_sup_simple, {"type": "choice", "instructions": _SUP_INSTR, "criteria": _SUP_SIMPLE}),
    "C4_英文criteria": (state_sup_orig, {"type": "choice", "instructions": _SUP_INSTR, "criteria": _SUP_EN}),
    "C5_直白criteria": (state_sup_orig, {"type": "choice", "instructions": _SUP_INSTR, "criteria": _SUP_DIRECT}),
}


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

    # ---------- A. page_fault: V2 vs V4 全面对决（dataset_v2 的 24 条） ----------
    print("=" * 78)
    print("A. page_fault：V2 vs V4 对决（dataset_v2 全量 24 条）")
    print("=" * 78)
    pf_samples = [s for s in v2 if s["task"] == "page_fault"]
    for vname, q in PF_VARIANTS.items():
        rows = []
        t_all = time.perf_counter()
        for s in pf_samples:
            state = f"【当前用户消息】\n{s['input']['query']}"
            r = router.predict(state, {"pf": q})
            a = r["answers"]["pf"]
            dec = "yes" if a["noul"] >= 0.5 else "no"
            rows.append({"id": s["id"], "difficulty": s["difficulty"], "gold": s["gold"],
                         "noul": a["noul"], "dec": dec, "ok": dec == s["gold"],
                         "trap": s.get("trap", ""), "query": s["input"]["query"]})
        acc = sum(r["ok"] for r in rows) / len(rows)
        ys = [r["noul"] for r in rows if r["gold"] == "yes"]
        ns = [r["noul"] for r in rows if r["gold"] == "no"]
        print(f"\n[{vname}] acc={acc:.3f}  耗时={time.perf_counter() - t_all:.1f}s")
        print(f"  noul 分布：yes组 中位数={statistics.median(ys):.3f} [{min(ys):.3f},{max(ys):.3f}] | "
              f"no组 中位数={statistics.median(ns):.3f} [{min(ns):.3f},{max(ns):.3f}]")
        wrong = [r for r in rows if not r["ok"]]
        if wrong:
            print(f"  误判 {len(wrong)} 条：")
            for r in wrong:
                print(f"    {r['id']} [{r['difficulty']}/{r.get('trap') or '-'}] gold={r['gold']} "
                      f"→ {r['dec']} (noul={r['noul']:.3f}) | {r['query']}")
        else:
            print("  全部正确")

    # ---------- B. supersede 第二轮变体 ----------
    print()
    print("=" * 78)
    print("B. supersede：state/criteria 变体（5 样例 × 4 变体）")
    print("=" * 78)
    sup_ids = ["sup_01", "sup_02", "sup_05", "sup_11", "sup_14"]
    for vname, (state_fn, q) in SUP_VARIANTS.items():
        hits = 0
        details = []
        for sid in sup_ids:
            s = v1[sid]
            r = router.predict(state_fn(s), {"sup": q})
            a = r["answers"]["sup"]
            ok = a["choice"] == s["gold"]
            hits += ok
            probs = {k: round(v, 2) for k, v in (a.get("probabilities") or {}).items()}
            details.append(f"    {sid} gold={s['gold']:9s} → {a['choice']:9s} {'✓' if ok else '✗'} {probs}")
        print(f"\n[{vname}] 命中 {hits}/5")
        print("\n".join(details))
    return 0


if __name__ == "__main__":
    sys.exit(main())
