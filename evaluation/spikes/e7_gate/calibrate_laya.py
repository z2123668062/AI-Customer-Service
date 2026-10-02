# -*- coding: utf-8 -*-
r"""laya 调用规格校准实验（E7 · 一次性受控实验，为 runner 定案表达方式）。

背景：v1 spike 在 laya 上异常（page_fault 16 条全部倒向 yes）。
源码取证（.venv/Lib/site-packages/laya/common.py build_sequence）：
  noul 的内部渲染是  "noul question: {instructions}"
  + 两个选项文本 "false: no, the statement does not hold" / "true: yes, the statement holds"，
  即判定的是 instructions 作为**陈述句命题**是否成立；
  且头部有 head_max_len=192 token 预算，超长指令可能被截断或稀释。

假设：长疑问句 instructions（含括号解释与“高概率”字样）污染判定。
实验矩阵（固定样例，只变措辞）：
  - page_fault: 4 样例 × 4 变体（原版长句 / 简洁疑问句 / 简洁陈述句 / 陈述句+criteria）
  - supersede : 4 样例 × 2 变体（原版 criteria / 简化 criteria）

运行（项目 venv，cwd=AI-Customer-Service）：
    $env:HF_ENDPOINT="https://hf-mirror.com"
    & "E:\毕设_记忆系统\.venv\Scripts\python.exe" -X utf8 `
        -m evaluation.spikes.e7_gate.calibrate_laya
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

SPIKE_DIR = Path(__file__).resolve().parent
DATASET = SPIKE_DIR / "dataset.jsonl"

# ---------- page_fault instructions 变体 ----------
_PF_LONG = (
    "用户当前消息是否需要调取该用户的长期历史记忆（过往对话中保存的关于本人的"
    "信息、经历、偏好、状态、约定）才能正确回应？引用了过去发生的事、用户说过的话，"
    "或需要用户的个人信息才能回答 → 需要记忆（高概率）；寒暄闲聊、通用知识问答、"
    "当场可完成的生成/翻译/计算任务、仅『可以更个性化』但当场就能正确回应的 → "
    "不需要记忆（低概率）。"
)
_PF_SHORT_Q = "回答该问题是否需要调取用户的历史记忆？"
_PF_STMT = "该消息必须依赖用户过往对话中的个人信息、经历、偏好或约定才能正确回应。"

PF_VARIANTS = {
    "V1_长疑问句(原版)": {"type": "noul", "instructions": _PF_LONG},
    "V2_简洁疑问句": {"type": "noul", "instructions": _PF_SHORT_Q},
    "V3_简洁陈述句": {"type": "noul", "instructions": _PF_STMT},
    "V4_陈述句+criteria": {
        "type": "noul",
        "instructions": _PF_STMT,
        "criteria": {"true": "需要查历史记忆", "false": "不需要历史记忆"},
    },
}

# ---------- supersede criteria 变体 ----------
_SUP_ORIG = {
    "unrelated": "主题不同，互不影响",
    "coexist": "补充或新增，两者可同时成立（含：主题相邻但属性不同、意向或计划尚未落实、同一对象的新事件）",
    "supersede": "新信息改写了旧卡片的同一属性，使旧内容过期失效",
}
_SUP_SIMPLE = {
    "unrelated": "主题不同，互不影响",
    "coexist": "主题相关，但两者可以同时成立（补充、新增或新事件）",
    "supersede": "同一属性被改写，旧内容过期失效",
}
SUP_VARIANTS = {
    "C1_原版criteria": {"type": "choice", "instructions": "新出现的信息与已有旧记忆卡片属于哪种关系？",
                        "criteria": _SUP_ORIG},
    "C2_简化criteria": {"type": "choice", "instructions": "新出现的信息与已有旧记忆卡片属于哪种关系？",
                        "criteria": _SUP_SIMPLE},
}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    samples = {}
    for ln in DATASET.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            s = json.loads(ln)
            samples[s["id"]] = s

    from laya import Router

    router = Router()
    t0 = time.perf_counter()
    router.predict("你好。", {"warm": {"type": "noul", "instructions": "这是一句问候。"}})
    print(f"预热 {1000 * (time.perf_counter() - t0):.0f}ms\n")

    print("=" * 78)
    print("A. page_fault：instructions 变体（gold 已标）")
    print("=" * 78)
    for sid in ("pf_01", "pf_02", "pf_09", "pf_10"):
        s = samples[sid]
        state = f"【当前用户消息】\n{s['input']['query']}"
        print(f"\n[{sid}] gold={s['gold']} | {s['input']['query']}")
        for vname, q in PF_VARIANTS.items():
            t0 = time.perf_counter()
            r = router.predict(state, {"pf": q})
            dt = (time.perf_counter() - t0) * 1000
            a = r["answers"]["pf"]
            dec = "yes" if a["noul"] >= 0.5 else "no"
            flag = "✓" if dec == s["gold"] else "✗"
            print(f"  {vname:22s} noul={a['noul']:.4f} → {dec} {flag}  "
                  f"(conf={a.get('answer_confidence'):.3f}, tokens={r.get('usage', {}).get('input_tokens')}, {dt:.0f}ms)")

    print()
    print("=" * 78)
    print("B. supersede：criteria 变体（gold 已标）")
    print("=" * 78)
    for sid in ("sup_01", "sup_05", "sup_11", "sup_14"):
        s = samples[sid]
        state = (f"【已有旧记忆卡片】\n{s['input']['old_card']}\n"
                 f"【新出现的信息】\n{s['input']['new_info']}")
        print(f"\n[{sid}] gold={s['gold']} | {s['input']['old_card']} → {s['input']['new_info']}")
        for vname, q in SUP_VARIANTS.items():
            t0 = time.perf_counter()
            r = router.predict(state, {"sup": q})
            dt = (time.perf_counter() - t0) * 1000
            a = r["answers"]["sup"]
            flag = "✓" if a["choice"] == s["gold"] else "✗"
            probs = {k: round(v, 3) for k, v in (a.get("probabilities") or {}).items()}
            print(f"  {vname:22s} choice={a['choice']} {flag}  probs={probs}  ({dt:.0f}ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
