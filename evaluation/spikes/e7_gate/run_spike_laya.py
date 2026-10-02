# -*- coding: utf-8 -*-
r"""E7 判定器 spike runner（laya 本地后端版）。

后端：laya（PyPI 包进程内推理，multilingual checkpoint，CPU）。
设计与流程权威文档：docs/判定器Spike文档.md。
数据集：
  - dataset.jsonl     v1（缺页/取代/回填 三题 46 条）——对照 deepseek-flash 的 92/92 基线
  - dataset_v2.jsonl  v2（四题 96 条，难度分层 easy/medium/hard，含陷阱标注与 rationale）
调用规格：经 3 轮受控校准（calibrate_laya{,2,3}.py）定案为 best-effort（见下方规格注释）。

用法（cwd 任意；需先设 HF_ENDPOINT 镜像）：
    $env:HF_ENDPOINT="https://hf-mirror.com"
    & "E:\毕设_记忆系统\.venv\Scripts\python.exe" -X utf8 -m evaluation.spikes.e7_gate.run_spike_laya
    ... --dataset dataset.jsonl   # 跑 v1 对照
    ... --repeat 2                # 重复 K 轮（稳定性回归）
    ... --limit 3                 # 每题只跑前 N 条冒烟

产物：evaluation/reports/e7_gate_spike/result_<ts>_laya_<stem>.json + report_<ts>_laya_<stem>.md
纪律：laya 为本地确定性推理，照抄 v1 的"三层校验 + fail-safe 兜底"方法论（校验失败不重试，直接兜底）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[3]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

SPIKE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "evaluation" / "reports" / "e7_gate_spike"
DEFAULT_DATASET = SPIKE_DIR / "dataset_v2.jsonl"

# ============ 判定映射规则（实验登记项：改映射 = 改实验条件）============
NOUL_THRESHOLD = 0.5              # noul ≥ 0.5 → yes
SLOT_LABELS = ("low", "mid", "high")   # score 档位：argmax(probabilities) → 0/1/2 档

# ============ 四个判断点的调用规格（经 3 轮受控校准定案的 best-effort 配置）============
# 校准结论（calibrate_laya{,2,3}.py；v1 首跑异常 → 源码取证 → 受控实验收敛）：
#   1) noul 的内部渲染为 "noul question: <instruction>" + 两项 criteria 文本（默认
#      "false: no, the statement does not hold" / "true: yes, the statement holds"），
#      判定语义是"<instruction> 作为命题是否成立"；超长疑问句会污染判定（v1 首跑全倒向 yes 的根因）。
#   2) 六种表述（长句/短问句/陈述句/陈述句+criteria/负向表述/choice 二选一）在 v2 全量上均不达标；
#      laya 官方 benchmark 注明 multilingual 非英语 MASSIVE-intent 0.451 / XNLI 0.731，
#      中文语用判定接近其能力上限——本规格为其中最稳者，用于完整登记实测数据。
_PAGE_FAULT_INSTRUCTIONS = "该消息必须依赖用户过往对话中的个人信息、经历、偏好或约定才能正确回应。"
_PAGE_FAULT_CRITERIA = {"true": "需要查历史记忆", "false": "不需要历史记忆"}
_SUPERSEDE_INSTRUCTIONS = "新出现的信息与已有旧记忆卡片属于哪种关系？"
_SUPERSEDE_CRITERIA = {
    "unrelated": "主题不同，互不影响",
    "coexist": "主题相关，但两者可以同时成立（补充、新增或新事件）",
    "supersede": "同一属性被改写，旧内容过期失效",
}
_BACKFILL_INSTRUCTIONS = "事件 A 与事件 B 之间存在明确的因果关系，可以写成一句『因为 A 所以 B』。"
_BACKFILL_CRITERIA = {"true": "存在因果", "false": "不存在因果"}
_SHORTLIST_INSTRUCTIONS = "候选记忆卡片与用户当前问题的相关程度属于哪一档？"
_SHORTLIST_CRITERIA = [
    "不相关：对回答没有帮助（含已过期的旧状态）",
    "部分相关：有参考价值，但不是回答所必需",
    "高度相关：直接构成或决定回答内容",
]

TASKS: dict[str, dict] = {
    "page_fault": {
        "label": "缺页判定（是否需要回源）",
        "qid": "page_fault",
        "type": "noul",
        "instructions": _PAGE_FAULT_INSTRUCTIONS,
        "criteria": _PAGE_FAULT_CRITERIA,
        "options": ("yes", "no"),
        "failsafe": "yes",
    },
    "supersede": {
        "label": "取代判定（无关/并存/取代）",
        "qid": "supersede",
        "type": "choice",
        "instructions": _SUPERSEDE_INSTRUCTIONS,
        "criteria": _SUPERSEDE_CRITERIA,
        "options": ("unrelated", "coexist", "supersede"),
        "failsafe": "coexist",
    },
    "backfill": {
        "label": "回填判定（是否存在因果）",
        "qid": "backfill",
        "type": "noul",
        "instructions": _BACKFILL_INSTRUCTIONS,
        "criteria": _BACKFILL_CRITERIA,
        "options": ("yes", "no"),
        "failsafe": "no",
    },
    "shortlist": {
        "label": "精筛判定（卡片相关档位）",
        "qid": "shortlist",
        "type": "score",
        "instructions": _SHORTLIST_INSTRUCTIONS,
        "criteria": _SHORTLIST_CRITERIA,
        "options": SLOT_LABELS,
        "failsafe": "low",
    },
}


def build_state(task_key: str, sample: dict) -> str:
    """把数据集样本拼成 laya 的 state 文本（含字段说明头，与 v1 输入字段对齐）。"""
    inp = sample["input"]
    if task_key == "page_fault":
        return f"【当前用户消息】\n{inp['query']}"
    if task_key == "supersede":
        return f"【已有旧记忆卡片】\n{inp['old_card']}\n【新出现的信息】\n{inp['new_info']}"
    if task_key == "backfill":
        return f"【事件 A】\n{inp['event_a']}\n【事件 B】\n{inp['event_b']}"
    if task_key == "shortlist":
        return f"【用户当前问题】\n{inp['query']}\n【候选记忆卡片】\n{inp['card']}"
    raise ValueError(f"unknown task {task_key!r}")


def build_questions(task_key: str) -> dict:
    spec = TASKS[task_key]
    q: dict = {"type": spec["type"], "instructions": spec["instructions"]}
    if "criteria" in spec:
        q["criteria"] = spec["criteria"]
    return {spec["qid"]: q}


# ============ 三层校验（laya 版：结构层 → 枚举/值域层 → 语义层）============
def _check_probs(probs: dict) -> str | None:
    """分布合法性；返回错误原因或 None。"""
    try:
        vals = [float(v) for v in probs.values()]
    except (TypeError, ValueError):
        return "semantic: 概率含非数值"
    if any(not (0.0 <= v <= 1.0) for v in vals):
        return "semantic: 概率越界"
    if abs(sum(vals) - 1.0) > 0.05:
        return f"semantic: 概率和不等于 1（{sum(vals):.4f}）"
    return None


def three_layer_check(task_key: str, result_obj: dict) -> tuple[dict | None, str | None]:
    """三层校验：结构层 → schema 层（枚举/字段）→ 语义层（值域/分布）。
    返回 (parsed, None) 或 (None, 失败原因)。parsed = {decision, confidence, detail}。"""
    spec = TASKS[task_key]
    qid, qtype = spec["qid"], spec["type"]
    # 结构层
    if not isinstance(result_obj, dict):
        return None, "format: 返回体不是 dict"
    answers = result_obj.get("answers")
    if not isinstance(answers, dict) or qid not in answers:
        return None, f"format: answers 缺少 {qid}"
    ans = answers[qid]
    if not isinstance(ans, dict):
        return None, "format: answer 不是 dict"

    if qtype == "noul":
        if "noul" not in ans:
            return None, "schema: 缺 noul 字段"
        try:
            p = float(ans["noul"])
        except (TypeError, ValueError):
            return None, f"schema: noul 非法 {ans['noul']!r}"
        if not (0.0 <= p <= 1.0):
            return None, f"semantic: noul 越界 {p}"
        conf = ans.get("answer_confidence")
        try:
            conf = float(conf) if conf is not None else max(p, 1.0 - p)
        except (TypeError, ValueError):
            conf = max(p, 1.0 - p)
        decision = "yes" if p >= NOUL_THRESHOLD else "no"
        return {"decision": decision, "confidence": round(conf, 4),
                "detail": {"prob_yes": round(p, 4)}}, None

    if qtype == "choice":
        ch = ans.get("choice")
        if not isinstance(ch, str):
            return None, "schema: 缺 choice 字段"
        if ch not in spec["options"]:
            return None, f"schema: choice 非法 {ch!r}"
        probs = ans.get("probabilities")
        detail: dict = {}
        if isinstance(probs, dict) and probs:
            err = _check_probs(probs)
            if err:
                return None, err
            detail["probabilities"] = {k: round(float(v), 4) for k, v in probs.items()}
        conf = ans.get("answer_confidence")
        try:
            conf = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf = 0.0
        return {"decision": ch, "confidence": round(conf, 4), "detail": detail}, None

    if qtype == "score":
        probs = ans.get("probabilities")
        if not isinstance(probs, dict) or not probs:
            return None, "schema: 缺 probabilities"
        if not set(probs) <= {"0", "1", "2"}:
            return None, f"schema: probabilities 键非法 {sorted(probs)}"
        err = _check_probs(probs)
        if err:
            return None, err
        top = max(probs.items(), key=lambda kv: float(kv[1]))[0]
        decision = SLOT_LABELS[int(top)]
        try:
            conf = float(probs[top])
        except (TypeError, ValueError):
            conf = 0.0
        detail = {"probabilities": {k: round(float(v), 4) for k, v in probs.items()}}
        if "score" in ans:
            try:
                detail["score_raw"] = round(float(ans["score"]), 4)
            except (TypeError, ValueError):
                pass
        return {"decision": decision, "confidence": round(conf, 4), "detail": detail}, None

    return None, f"format: 未知题型 {qtype}"


# ============ 正确性判定 ============
def judge_correct(task_key: str, decision: str | None, gold: str) -> tuple[bool | None, bool | None]:
    """返回 (exact_correct, binary_correct)。binary 仅精筛任务使用（相关/不相关二分）。"""
    if decision is None:
        return None, None
    if TASKS[task_key]["type"] == "score":
        gi, pi = SLOT_LABELS.index(gold), SLOT_LABELS.index(decision)
        return (pi == gi), ((pi >= 1) == (gi >= 1))
    return (decision == gold), (decision == gold)


# ============ 主循环 ============
def warmup(router) -> float:
    """预热：触发模型加载与首次推理，避免首条样例延迟污染统计。"""
    t0 = time.perf_counter()
    router.predict("【当前用户消息】\n你好。", build_questions("page_fault"))
    return (time.perf_counter() - t0) * 1000


def run(router, samples: list[dict], repeat: int) -> tuple[list[dict], dict]:
    rounds: list[list[dict]] = []
    for r in range(1, repeat + 1):
        records: list[dict] = []
        total = len(samples)
        for i, sample in enumerate(samples, 1):
            task_key, spec = sample["task"], TASKS[sample["task"]]
            rec: dict = {"task": task_key, "id": sample["id"], "gold": sample["gold"],
                         "difficulty": sample.get("difficulty", "v1"),
                         "trap": sample.get("trap", ""),
                         "rationale": sample.get("rationale", sample.get("note", "")),
                         "input": sample["input"]}
            try:
                t0 = time.perf_counter()
                result = router.predict(build_state(task_key, sample), build_questions(task_key))
                rec["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                rec["routing_model"] = (result.get("routing") or {}).get("model")
                usage = result.get("usage") or {}
                rec["input_tokens"] = usage.get("input_tokens")
                parsed, fail_reason = three_layer_check(task_key, result)
            except Exception as e:  # noqa: BLE001 - 推理失败计入 fail-safe
                parsed, fail_reason = None, f"call: {type(e).__name__}: {e}"
            if parsed is None:
                rec.update(valid=False, fail_reason=fail_reason, decision=None,
                           confidence=None, detail={})
                rec["effective_decision"] = spec["failsafe"]
            else:
                rec.update(valid=True, fail_reason=None, decision=parsed["decision"],
                           confidence=parsed["confidence"], detail=parsed["detail"])
                rec["effective_decision"] = parsed["decision"]
            exact, binary = judge_correct(task_key, rec["decision"], rec["gold"])
            rec["correct"] = exact
            rec["binary_correct"] = binary
            rec["effective_correct"] = (rec["effective_decision"] == rec["gold"])
            flag = "✓" if rec["effective_correct"] else "✗"
            mark = "" if rec["valid"] else " [fail-safe]"
            prefix = f"[r{r}] " if repeat > 1 else ""
            print(f"{prefix}[{i}/{total}] {task_key:11s} {sample['id']} {flag}{mark}"
                  + (f"  {rec.get('fail_reason')}" if not rec["valid"] else ""))
            records.append(rec)
        rounds.append(records)

    # 稳定性：各条在 K 轮间判定是否完全一致
    stability: dict = {"repeat": repeat, "overall": None, "by_task": {}, "unstable_ids": []}
    if repeat > 1:
        base = rounds[0]
        unstable = []
        per_task_total: dict[str, int] = {}
        per_task_stable: dict[str, int] = {}
        for idx, rec0 in enumerate(base):
            t = rec0["task"]
            per_task_total[t] = per_task_total.get(t, 0) + 1
            decisions = [rd[idx]["effective_decision"] for rd in rounds]
            if len(set(decisions)) == 1:
                per_task_stable[t] = per_task_stable.get(t, 0) + 1
            else:
                unstable.append({"id": rec0["id"], "task": t, "decisions": decisions})
        stability["overall"] = round(sum(per_task_stable.values()) / max(len(base), 1), 4)
        stability["by_task"] = {t: round(per_task_stable.get(t, 0) / n, 4)
                                for t, n in per_task_total.items()}
        stability["unstable_ids"] = unstable
    return rounds[0], stability


# ============ 指标统计 ============
def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return round(vs[int(k)], 1)
    return round(vs[f] + (vs[c] - vs[f]) * (k - f), 1)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(records: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for task_key in TASKS:
        rs = [r for r in records if r["task"] == task_key]
        if not rs:
            continue
        valid = [r for r in rs if r["valid"]]
        s: dict = {
            "label": TASKS[task_key]["label"],
            "n": len(rs),
            "valid_n": len(valid),
            "accuracy": round(sum(1 for r in valid if r["correct"]) / len(valid), 4) if valid else None,
            "effective_accuracy": round(sum(1 for r in rs if r["effective_correct"]) / len(rs), 4),
            "parse_success_rate": round(len(valid) / len(rs), 4),
            "failsafe_rate": round(1 - len(valid) / len(rs), 4),
            "latency_p50_ms": _percentile([r["latency_ms"] for r in rs if "latency_ms" in r], 50),
            "latency_p95_ms": _percentile([r["latency_ms"] for r in rs if "latency_ms" in r], 95),
            "avg_answer_confidence": _mean([r["confidence"] for r in valid if r.get("confidence") is not None]),
            "avg_input_tokens": _mean([r["input_tokens"] for r in rs if r.get("input_tokens") is not None]),
        }
        # 分层准确率（v2 新增）
        diff_stats: dict[str, dict] = {}
        for d in ("easy", "medium", "hard", "v1"):
            d_rs = [r for r in valid if r["difficulty"] == d]
            if d_rs:
                diff_stats[d] = {"n": len(d_rs),
                                 "accuracy": round(sum(1 for r in d_rs if r["correct"]) / len(d_rs), 4)}
        s["by_difficulty"] = diff_stats
        # 任务专项
        if task_key == "page_fault":
            s["dangerous_miss"] = sum(1 for r in rs if r["effective_decision"] == "no" and r["gold"] == "yes")
            s["safe_extra"] = sum(1 for r in rs if r["effective_decision"] == "yes" and r["gold"] == "no")
        if task_key == "supersede":
            conf_pairs: dict[str, int] = {}
            for r in valid:
                if not r["correct"]:
                    key = f"{r['gold']}→{r['decision']}"
                    conf_pairs[key] = conf_pairs.get(key, 0) + 1
            s["confusion"] = conf_pairs
            s["missed_supersede"] = sum(1 for r in valid if r["gold"] == "supersede" and r["decision"] != "supersede")
            s["wrong_supersede"] = sum(1 for r in valid if r["gold"] != "supersede" and r["decision"] == "supersede")
        if task_key == "backfill":
            s["dangerous_connect"] = sum(1 for r in valid if r["gold"] == "no" and r["decision"] == "yes")
            s["missed_link"] = sum(1 for r in valid if r["gold"] == "yes" and r["decision"] == "no")
        if task_key == "shortlist":
            s["binary_accuracy"] = round(sum(1 for r in valid if r["binary_correct"]) / len(valid), 4) if valid else None
            s["noise_injection"] = sum(1 for r in valid if r["gold"] == "low" and r["decision"] != "low")
            s["missed_relevant"] = sum(1 for r in valid if r["gold"] in ("mid", "high") and r["decision"] == "low")
        stats[task_key] = s
    return stats


def make_verdict(stats: dict[str, dict]) -> str:
    accs = {k: s["accuracy"] for k, s in stats.items() if s["accuracy"] is not None}
    min_acc = min(accs.values()) if accs else 0.0
    min_parse = min(s["parse_success_rate"] for s in stats.values())
    lines = []
    if min_acc >= 0.85 and min_parse >= 0.95:
        lines.append("**机器建议：达标** —— 全部任务 accuracy ≥ 0.85 且 parse_success_rate ≥ 0.95，"
                     "laya 本地后端可作为 gate.py 判定后端（仍需人工核延迟与集成形态）。")
    elif min_acc >= 0.80:
        lines.append("**机器建议：基本达标** —— accuracy ≥ 0.80，建议对最低任务补确定性前置或微调指令文案后复测。")
    elif min_acc >= 0.70:
        lines.append("**机器建议：部分降级** —— 存在任务 accuracy < 0.70，该判断点需补强或换升级档。")
    else:
        lines.append("**机器建议：未达标** —— 多任务 accuracy < 0.70，需重新评估。")
    low = [f"{k}={v}" for k, v in accs.items() if v < 0.85]
    if low:
        lines.append(f"未达冲刺线（0.85）的任务：{', '.join(low)}")
    return "\n".join(lines)


# ============ 报告落盘 ============
def write_outputs(records: list[dict], stats: dict, stability: dict, ts: str,
                  dataset_path: Path, dataset_hash: str, repeat: int,
                  warmup_ms: float) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = dataset_path.stem
    try:
        import importlib.metadata as im
        laya_version = im.version("laya")
    except Exception:  # noqa: BLE001
        laya_version = "unknown"
    task_specs = {k: {"type": v["type"], "instructions": v["instructions"],
                      "criteria": v.get("criteria")} for k, v in TASKS.items()}
    meta = {
        "backend": f"laya(PyPI {laya_version}, multilingual checkpoint, torch CPU)",
        "hf_endpoint": os.environ.get("HF_ENDPOINT", "(unset)"),
        "mapping_rules": {"noul": f"prob>= {NOUL_THRESHOLD} -> yes",
                          "choice": "take choice field",
                          "score": "argmax(probabilities) -> low/mid/high"},
        "spec_calibration": "3 轮受控校准定案 best-effort 规格；官方 multilingual 非英语 "
                            "MASSIVE-intent 0.451 / XNLI 0.731 为能力边界佐证",
        "dataset": str(dataset_path.relative_to(BASE_DIR)),
        "dataset_sha256_12": dataset_hash,
        "n_samples": len(records),
        "repeat": repeat,
        "warmup_ms": round(warmup_ms, 1),
        "run_at": ts,
        "retry": 0,
        "task_specs": task_specs,
    }
    result_path = REPORT_DIR / f"result_{ts}_laya_{stem}.json"
    result_path.write_text(
        json.dumps({"meta": meta, "stats": stats, "stability": stability, "records": records},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# E7 判定器 Spike 报告 · laya 本地后端（{ts}）", "",
             "> 后端：laya 本地模型（进程内推理）｜设计与流程：docs/判定器Spike文档.md。",
             "> 判定映射已登记于本报告头部；结论需人工最终拍板。",
             "> 调用规格经 3 轮受控校准（calibrate_laya{,2,3}.py）定为 best-effort；"
             "官方 benchmark：multilingual 非英语 MASSIVE-intent 0.451 / XNLI 0.731（能力边界佐证）。", "",
             "## 1. 环境登记（实验条件）", "",
             f"- 后端：`{meta['backend']}`",
             f"- HF 镜像：`{meta['hf_endpoint']}`",
             f"- 判定映射：noul(p≥{NOUL_THRESHOLD}→yes) / choice 取字段 / score argmax→档位",
             f"- 数据集：`{meta['dataset']}`（sha256前12位 `{dataset_hash}`，共 {len(records)} 条，repeat={repeat}）",
             f"- 预热耗时：{meta['warmup_ms']}ms（模型加载+首推理，不计入延迟统计）；运行时间：{ts}", "",
             "## 2. 汇总指标", "",
             "| 任务 | n | accuracy | effective_acc | parse | failsafe | P50(ms) | P95(ms) | avg_conf | avg_tokens |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in stats.items():
        lines.append(f"| {s['label']} | {s['n']} | {s['accuracy']} | {s['effective_accuracy']} | "
                     f"{s['parse_success_rate']} | {s['failsafe_rate']} | {s['latency_p50_ms']} | "
                     f"{s['latency_p95_ms']} | {s['avg_answer_confidence']} | {s['avg_input_tokens']} |")

    lines += ["", "## 3. 难度分层准确率（v2 价值区：easy→hard 梯度）", "",
              "| 任务 | easy | medium | hard |", "|---|---|---|---|"]
    for k, s in stats.items():
        d = s.get("by_difficulty", {})
        row = " | ".join(
            f"{d.get(x, {}).get('accuracy', '—')} (n={d.get(x, {}).get('n', 0)})"
            for x in ("easy", "medium", "hard"))
        lines.append(f"| {s['label']} | {row} |")

    lines += ["", "## 4. 任务专项指标（误差方向分解）", ""]
    pf = stats.get("page_fault", {})
    if pf:
        lines += [f"- **缺页**：`dangerous_miss`（该回忆没回忆）= {pf.get('dangerous_miss', 0)}；"
                  f"`safe_extra`（多回源）= {pf.get('safe_extra', 0)}"]
    sp = stats.get("supersede", {})
    if sp:
        lines += [f"- **取代**：`missed_supersede`（该取代未取代，旧卡滞留）= {sp.get('missed_supersede', 0)}；"
                  f"`wrong_supersede`（误取代，信息丢失）= {sp.get('wrong_supersede', 0)}；"
                  f"混淆对：{sp.get('confusion') or '无'}"]
    bf = stats.get("backfill", {})
    if bf:
        lines += [f"- **回填**：`dangerous_connect`（错连线）= {bf.get('dangerous_connect', 0)}；"
                  f"`missed_link`（漏连线）= {bf.get('missed_link', 0)}"]
    sl = stats.get("shortlist", {})
    if sl:
        lines += [f"- **精筛**：二值准确率（相关/不相关）= {sl.get('binary_accuracy')}；"
                  f"`noise_injection`（低档误升，注入噪声风险）= {sl.get('noise_injection', 0)}；"
                  f"`missed_relevant`（相关误降，漏注入）= {sl.get('missed_relevant', 0)}"]

    if repeat > 1:
        lines += ["", "## 5. 重复稳定性（laya 确定性验证）", "",
                  f"- 总体一致率：**{stability.get('overall')}**（{repeat} 轮判定完全一致的比例）",
                  f"- 分任务：{stability.get('by_task')}"]
        if stability.get("unstable_ids"):
            lines += ["- 不一致条目："] + [
                f"  - `{u['id']}`（{u['task']}）：{u['decisions']}" for u in stability["unstable_ids"]]
        else:
            lines += ["- 无不一致条目（全部样例在重复轮次间判定稳定）"]

    wrong = [r for r in records if r["valid"] and not r["correct"]]
    failsafe_rs = [r for r in records if not r["valid"]]
    sec = 6 if repeat > 1 else 5
    lines += ["", f"## {sec}. 错例与兜底明细", ""]
    if wrong:
        lines += [f"### {sec}.1 判定错误（校验通过但判错）", "",
                  "| 任务 | id | 难度 | gold | 判定 | 陷阱 | rationale |", "|---|---|---|---|---|---|---|"]
        for r in wrong:
            lines.append(f"| {r['task']} | {r['id']} | {r['difficulty']} | {r['gold']} | "
                         f"{r['decision']} | {r.get('trap') or '—'} | {r.get('rationale', '')} |")
    else:
        lines.append("（无判定错误）")
    if failsafe_rs:
        lines += ["", f"### {sec}.2 fail-safe 兜底（三层校验未通过）", "",
                  "| 任务 | id | gold | 兜底方向 | 失败原因 |", "|---|---|---|---|---|"]
        for r in failsafe_rs:
            lines.append(f"| {r['task']} | {r['id']} | {r['gold']} | {r['effective_decision']} | "
                         f"{r['fail_reason']} |")
    else:
        lines += ["", "（无兜底样本：三层校验 100% 通过）"]

    lines += ["", f"## {sec + 1}. 拍板结论", "", make_verdict(stats), "",
              "> ⚠️ 本节为机器建议，最终拍板与决策记录（Dxx）由人工写入 docs/决策记录.md。"]
    report_path = REPORT_DIR / f"report_{ts}_laya_{stem}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result_path, report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="E7 判定器 spike（laya 本地后端）")
    parser.add_argument("--dataset", type=str, default=str(DEFAULT_DATASET),
                        help="数据集 jsonl 路径（默认 dataset_v2.jsonl）")
    parser.add_argument("--limit", type=int, default=0, help="每题只跑前 N 条（0=全量）")
    parser.add_argument("--repeat", type=int, default=1, help="重复轮数（稳定性回归）")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = SPIKE_DIR / dataset_path
    samples = [json.loads(ln) for ln in dataset_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.limit > 0:
        per_task: dict[str, int] = {}
        limited = []
        for s in samples:
            per_task[s["task"]] = per_task.get(s["task"], 0) + 1
            if per_task[s["task"]] <= args.limit:
                limited.append(s)
        samples = limited

    dataset_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()[:12]
    print(f"== E7 判定器 spike（laya 本地后端）：{len(samples)} 条 / dataset={dataset_hash} / "
          f"repeat={args.repeat} / HF_ENDPOINT={os.environ.get('HF_ENDPOINT', '(unset)')} ==")

    from laya import Router
    router = Router()
    warmup_ms = warmup(router)
    print(f"-- 预热完成（{warmup_ms:.0f}ms），开始正式评测 --")

    records, stability = run(router, samples, args.repeat)
    stats = summarize(records)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path, report_path = write_outputs(records, stats, stability, ts, dataset_path,
                                             dataset_hash, args.repeat, warmup_ms)

    print("\n== 汇总 ==")
    for k, s in stats.items():
        print(f"{k:11s} acc={s['accuracy']} eff_acc={s['effective_accuracy']} "
              f"parse={s['parse_success_rate']} p50={s['latency_p50_ms']}ms p95={s['latency_p95_ms']}ms")
    if args.repeat > 1:
        print(f"稳定性一致率：{stability['overall']}")
    print(f"\n机器建议：{make_verdict(stats).splitlines()[0]}")
    print(f"产物：\n  {result_path}\n  {report_path}")


if __name__ == "__main__":
    main()
