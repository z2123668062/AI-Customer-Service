# -*- coding: utf-8 -*-
r"""E7 判定器 spike runner（OpenAI 兼容 API · 多后端参数化版）。

用途：同一 v2 数据集（四任务 96 条）× 同一套"约束模式"prompt，仅替换模型后端，
实测「延迟 + 准确率」——为"小型模型能否替代 deepseek-flash 做判定"提供同条件对照。

后端：任意 OpenAI 兼容端点，通过 --model/--base-url/--key 切换：
  - 默认取 .env 的 LLM_*（deepseek-flash 基线）；
  - 硅基流动（api.siliconflow.cn）复用 EMBEDDING_API_KEY（--key embedding）。

用法（cwd 任意，脚本自行定位项目根）：
    # deepseek-flash 基线（v2 数据集）
    & "E:\毕设_记忆系统\.venv\Scripts\python.exe" -X utf8 -m evaluation.spikes.e7_gate.run_spike_api
    # 硅基流动小模型
    ... -m evaluation.spikes.e7_gate.run_spike_api --model Qwen/Qwen3-8B `
        --base-url https://api.siliconflow.cn/v1 --key embedding
    ... --limit 2 --no-report        # 冒烟（不落盘）
    ... --enable-thinking off        # Qwen3 系关思考（extra_body）
    ... --dataset dataset.jsonl      # 跑 v1 对照

产物：evaluation/reports/e7_gate_spike/result_<ts>_api_<slug>_<stem>.json + report_<ts>_api_<slug>_<stem>.md
纪律：不做重试（测裸结构化稳定性）；校验失败按 fail-safe 兜底（与 D19 方法论一致）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

# ---- 项目根定位（本文件: AI-Customer-Service/evaluation/spikes/e7_gate/run_spike_api.py）----
BASE_DIR = Path(__file__).resolve().parents[3]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from openai import OpenAI  # noqa: E402

from app.core.config import settings  # noqa: E402

SPIKE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "evaluation" / "reports" / "e7_gate_spike"
DEFAULT_DATASET = SPIKE_DIR / "dataset_v2.jsonl"

# ============ 四个判断点的判定模式（与 D19 定案一致：极简 prompt + 枚举写死 + 边界反例）============

_PAGE_FAULT_SYSTEM = """你是个人助手的记忆检索分诊判定器。只做一件事：判断"用户这句话"是否需要回顾历史记忆（回源检索）才能正确回应。

判定标准：
- yes（需要回源）：句子引用了过去发生的事或用户说过的信息（如"上次""之前""还记得""我提过"）；句子需要用户的个人信息才能回答（名字、住址、偏好、目标、人际关系、经历等）。
- no（不需要回源）：寒暄闲聊、通用知识问答、当场可完成的生成/翻译/计算任务。

判断示例：
- 输入"你还记得我上次说的搬家计划吗？" → yes（引用历史）
- 输入"今天天气怎么样？" → no（闲聊，无需记忆）

只输出一个 json 对象，不要输出任何其他文字。格式：
{"decision": "yes 或 no", "confidence": 0到1之间的小数, "reason": "一句话理由"}"""

_SUPERSEDE_SYSTEM = """你是记忆写入系统的去重裁决器。给你一张已存在的旧卡片和一条从新对话中抽出的新信息，判断两者的关系：
- unrelated：主题不同，互不影响
- coexist：新信息是补充或新增，两者可同时成立（如又养了第二只宠物、学会新技能）
- supersede：新信息改写了旧卡片的同一个属性，旧内容已过期（如搬家、换工作、改目标）

判断示例：
- 旧卡片"用户住在上海浦东" + 新信息"用户搬到了北京朝阳" → supersede（同一属性被改写）
- 旧卡片"用户会说英语" + 新信息"用户还会说日语" → coexist（能力叠加）
- 旧卡片"用户住在上海" + 新信息"用户喜欢吃辣" → unrelated（主题不同）

只输出一个 json 对象，不要输出任何其他文字。格式：
{"decision": "unrelated 或 coexist 或 supersede", "confidence": 0到1之间的小数, "reason": "一句话理由"}"""

_BACKFILL_SYSTEM = """你是记忆系统的因果回填判定器。给你两条事件记忆 A 和 B，判断两者之间是否存在明确、可解释的因果关系（B 是 A 的原因，或 A 是 B 的原因）。
- yes：存在明确因果，能用一句话解释"因为 B 所以 A"或"因为 A 所以 B"
- no：仅仅时间相邻、主题无关、或无法给出合理解释的关联（错误的因果链比缺失更危险）

判断示例：
- A"用户开始每天早上跑步" + B"用户体检发现血脂偏高，医生建议多运动" → yes（健康问题促使开始运动）
- A"用户买了一台新电脑" + B"用户周末去了动物园" → no（时间相邻但无因果解释）

只输出一个 json 对象，不要输出任何其他文字。格式：
{"decision": "yes 或 no", "confidence": 0到1之间的小数, "reason": "一句话理由"}"""

_SHORTLIST_SYSTEM = """你是记忆注入前的精筛判定器。给你用户当前的问题和一张候选记忆卡片，判断这张卡片对回答问题的价值档位：
- low：不相关，对回答没有帮助（包括已过期的旧状态）
- mid：部分相关，有参考价值但不是回答所必需
- high：高度相关，直接构成或决定回答内容

判断示例：
- 问题"我周末去哪露营好？" + 卡片"用户两个月前买过一顶帐篷" → mid（有关联但非必需）
- 问题"我对什么坚果过敏？" + 卡片"用户对花生严重过敏" → high（直接决定回答）
- 问题"推荐一部电影" + 卡片"用户住在上海浦东新区" → low（与问题无关）

只输出一个 json 对象，不要输出任何其他文字。格式：
{"decision": "low 或 mid 或 high", "confidence": 0到1之间的小数, "reason": "一句话理由"}"""

TASKS: dict[str, dict] = {
    "page_fault": {
        "label": "缺页判定（是否需要回源）",
        "system": _PAGE_FAULT_SYSTEM,
        "user_template": "用户消息：{query}",
        "options": ("yes", "no"),
        "failsafe": "yes",
    },
    "supersede": {
        "label": "取代判定（无关/并存/取代）",
        "system": _SUPERSEDE_SYSTEM,
        "user_template": "旧卡片：{old_card}\n新信息：{new_info}",
        "options": ("unrelated", "coexist", "supersede"),
        "failsafe": "coexist",
    },
    "backfill": {
        "label": "回填判定（是否存在因果）",
        "system": _BACKFILL_SYSTEM,
        "user_template": "事件 A：{event_a}\n事件 B：{event_b}",
        "options": ("yes", "no"),
        "failsafe": "no",
    },
    "shortlist": {
        "label": "精筛判定（卡片相关档位）",
        "system": _SHORTLIST_SYSTEM,
        "user_template": "用户当前问题：{query}\n候选记忆卡片：{card}",
        "options": ("low", "mid", "high"),
        "failsafe": "low",
    },
}

SLOT_LABELS = ("low", "mid", "high")  # 精筛档位（binary 判定用）


# ============ 三层校验（格式 → schema → 语义；与 D19 定案的 run_spike.py 一致）============
def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t[3:]
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3].rstrip()
    return t


def three_layer_check(task_key: str, raw: str) -> tuple[dict | None, str | None]:
    try:
        obj = json.loads(_strip_fence(raw))
    except Exception as e:  # noqa: BLE001
        return None, f"format: JSON 解析失败({e})"
    if not isinstance(obj, dict):
        return None, "format: 顶层不是 json 对象"
    missing = [k for k in ("decision", "confidence", "reason") if k not in obj]
    if missing:
        return None, f"schema: 缺字段 {missing}"
    decision = obj["decision"]
    if not isinstance(decision, str) or decision.strip().lower() not in TASKS[task_key]["options"]:
        return None, f"schema: decision 非法 {decision!r}"
    try:
        conf = float(obj["confidence"])
    except (TypeError, ValueError):
        return None, f"schema: confidence 非法 {obj['confidence']!r}"
    if not (0.0 <= conf <= 1.0):
        return None, f"semantic: confidence 越界 {conf}"
    if not isinstance(obj["reason"], str) or not obj["reason"].strip():
        return None, "semantic: reason 为空"
    return {"decision": decision.strip().lower(), "confidence": round(conf, 4),
            "reason": obj["reason"].strip()}, None


# ============ 正确性判定 ============
def judge_correct(task_key: str, decision: str | None, gold: str) -> tuple[bool | None, bool | None]:
    """返回 (exact_correct, binary_correct)。binary 仅精筛任务使用（相关/不相关二分）。"""
    if decision is None:
        return None, None
    if task_key == "shortlist":
        gi, pi = SLOT_LABELS.index(gold), SLOT_LABELS.index(decision)
        return (pi == gi), ((pi >= 1) == (gi >= 1))
    return (decision == gold), (decision == gold)


# ============ 调用与主循环 ============
def call_judge(client: OpenAI, model: str, task_key: str, sample: dict,
               json_mode: bool, extra_body: dict | None):
    spec = TASKS[task_key]
    messages = [
        {"role": "system", "content": spec["system"]},
        {"role": "user", "content": spec["user_template"].format(**sample["input"])},
    ]
    kwargs: dict = dict(model=model, messages=messages, temperature=0.0, top_p=0.1)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if extra_body:
        kwargs["extra_body"] = extra_body
    t0 = time.perf_counter()
    resp = client.chat.completions.create(**kwargs, timeout=60)
    latency_ms = (time.perf_counter() - t0) * 1000
    return resp.choices[0].message.content or "", latency_ms, resp.usage


def run(client: OpenAI, model: str, samples: list[dict], json_mode: bool,
        extra_body: dict | None) -> list[dict]:
    records: list[dict] = []
    total = len(samples)
    for i, sample in enumerate(samples, 1):
        task_key, spec = sample["task"], TASKS[sample["task"]]
        rec: dict = {"task": task_key, "id": sample["id"], "gold": sample["gold"],
                     "difficulty": sample.get("difficulty", "v1"),
                     "trap": sample.get("trap", ""),
                     "rationale": sample.get("rationale", sample.get("note", ""))}
        try:
            raw, latency_ms, usage = call_judge(client, model, task_key, sample, json_mode, extra_body)
            rec["latency_ms"] = round(latency_ms, 1)
            rec["raw"] = raw[:500]
            if usage is not None:
                rec["prompt_tokens"] = usage.prompt_tokens
                rec["completion_tokens"] = usage.completion_tokens
            parsed, fail_reason = three_layer_check(task_key, raw)
        except Exception as e:  # noqa: BLE001 - 调用失败计入 fail-safe
            parsed, fail_reason = None, f"call: {type(e).__name__}: {e}"
            rec["raw"] = ""
        if parsed is None:
            rec.update(valid=False, fail_reason=fail_reason, decision=None,
                       confidence=None, reason="")
            rec["effective_decision"] = spec["failsafe"]
        else:
            rec.update(valid=True, fail_reason=None, decision=parsed["decision"],
                       confidence=parsed["confidence"], reason=parsed["reason"])
            rec["effective_decision"] = parsed["decision"]
        exact, binary = judge_correct(task_key, rec["decision"], rec["gold"])
        rec["correct"] = exact
        rec["binary_correct"] = binary
        rec["effective_correct"] = (rec["effective_decision"] == rec["gold"])
        flag = "✓" if rec["effective_correct"] else "✗"
        mark = "" if rec["valid"] else " [fail-safe]"
        print(f"[{i}/{total}] {task_key:11s} {sample['id']} {flag}{mark}"
              + (f"  {rec.get('fail_reason')}" if not rec["valid"] else ""))
        records.append(rec)
    return records


# ============ 指标统计（与 laya 版口径一致：分层 + 误差方向）============
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
            "avg_latency_ms": _mean([r["latency_ms"] for r in rs if "latency_ms" in r]),
            "avg_prompt_tokens": _mean([r["prompt_tokens"] for r in rs if "prompt_tokens" in r]),
            "avg_completion_tokens": _mean([r["completion_tokens"] for r in rs if "completion_tokens" in r]),
        }
        diff_stats: dict[str, dict] = {}
        for d in ("easy", "medium", "hard", "v1"):
            d_rs = [r for r in valid if r["difficulty"] == d]
            if d_rs:
                diff_stats[d] = {"n": len(d_rs),
                                 "accuracy": round(sum(1 for r in d_rs if r["correct"]) / len(d_rs), 4)}
        s["by_difficulty"] = diff_stats
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
                     "该后端可作为 gate.py 判定候选（仍需人工核延迟与总体一致性）。")
    elif min_acc >= 0.80:
        lines.append("**机器建议：基本达标** —— accuracy ≥ 0.80，需对最低任务补确定性前置或调整文案后复测。")
    elif min_acc >= 0.70:
        lines.append("**机器建议：部分降级** —— 存在任务 accuracy < 0.70，该判断点需补强或换档。")
    else:
        lines.append("**机器建议：未达标** —— 多任务 accuracy < 0.70，需重新评估。")
    low = [f"{k}={v}" for k, v in accs.items() if v < 0.85]
    if low:
        lines.append(f"未达冲刺线（0.85）的任务：{', '.join(low)}")
    return "\n".join(lines)


# ============ 报告落盘 ============
def _slug(model: str) -> str:
    return model.split("/")[-1].lower().replace(" ", "-")


def write_outputs(records: list[dict], stats: dict, ts: str, dataset_path: Path,
                  dataset_hash: str, model: str, base_url: str, json_mode: bool,
                  extra_body: dict | None) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = dataset_path.stem
    slug = _slug(model)
    meta = {
        "backend": "openai-compatible-api",
        "model": model,
        "base_url": base_url,
        "temperature": 0.0,
        "top_p": 0.1,
        "json_mode": json_mode,
        "extra_body": extra_body,
        "dataset": str(dataset_path.relative_to(BASE_DIR)),
        "dataset_sha256_12": dataset_hash,
        "n_samples": len(records),
        "run_at": ts,
        "retry": 0,
    }
    result_path = REPORT_DIR / f"result_{ts}_api_{slug}_{stem}.json"
    result_path.write_text(
        json.dumps({"meta": meta, "stats": stats, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    lines = [f"# E7 判定器 Spike 报告 · API 后端 `{model}`（{ts}）", "",
             "> 设计：docs/判定器Spike文档.md ｜ prompt=四任务约束模式（与 D19/shortlist 登记一致）。",
             "> 目的：同数据集同 prompt 下换模型，测「延迟 + 准确率」小模型对照。", "",
             "## 1. 环境登记（实验条件）", "",
             f"- 模型：`{model}` @ `{base_url}`",
             f"- 判定模式：temperature=0.0 / top_p=0.1 / json_mode={json_mode} / "
             f"extra_body={extra_body or '无'} / 重试=0 次",
             f"- 数据集：`{meta['dataset']}`（sha256前12位 `{dataset_hash}`，共 {len(records)} 条）",
             f"- 运行时间：{ts}", "",
             "## 2. 汇总指标（含延迟——本次实验的焦点）", "",
             "| 任务 | n | accuracy | eff_acc | parse | failsafe | P50(ms) | P95(ms) | avg(ms) | prompt tok | completion tok |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in stats.items():
        lines.append(f"| {s['label']} | {s['n']} | {s['accuracy']} | {s['effective_accuracy']} | "
                     f"{s['parse_success_rate']} | {s['failsafe_rate']} | {s['latency_p50_ms']} | "
                     f"{s['latency_p95_ms']} | {s['avg_latency_ms']} | {s['avg_prompt_tokens']} | "
                     f"{s['avg_completion_tokens']} |")

    lines += ["", "## 3. 难度分层准确率（easy→hard 梯度）", "",
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
        lines += [f"- **取代**：`missed_supersede`（旧卡滞留）= {sp.get('missed_supersede', 0)}；"
                  f"`wrong_supersede`（误取代信息丢失）= {sp.get('wrong_supersede', 0)}；"
                  f"混淆对：{sp.get('confusion') or '无'}"]
    bf = stats.get("backfill", {})
    if bf:
        lines += [f"- **回填**：`dangerous_connect`（错连线）= {bf.get('dangerous_connect', 0)}；"
                  f"`missed_link`（漏连线）= {bf.get('missed_link', 0)}"]
    sl = stats.get("shortlist", {})
    if sl:
        lines += [f"- **精筛**：二值准确率（相关/不相关）= {sl.get('binary_accuracy')}；"
                  f"`noise_injection`（低档误升）= {sl.get('noise_injection', 0)}；"
                  f"`missed_relevant`（相关误降）= {sl.get('missed_relevant', 0)}"]

    wrong = [r for r in records if r["valid"] and not r["correct"]]
    failsafe_rs = [r for r in records if not r["valid"]]
    lines += ["", "## 5. 错例与兜底明细", ""]
    if wrong:
        lines += ["### 5.1 判定错误（校验通过但判错）", "",
                  "| 任务 | id | 难度 | gold | 判定 | 陷阱 | rationale |", "|---|---|---|---|---|---|---|"]
        for r in wrong:
            lines.append(f"| {r['task']} | {r['id']} | {r['difficulty']} | {r['gold']} | "
                         f"{r['decision']} | {r.get('trap') or '—'} | {r.get('rationale', '')} |")
    else:
        lines.append("（无判定错误）")
    if failsafe_rs:
        lines += ["", "### 5.2 fail-safe 兜底（三层校验未通过）", "",
                  "| 任务 | id | gold | 兜底方向 | 失败原因 |", "|---|---|---|---|---|"]
        for r in failsafe_rs:
            lines.append(f"| {r['task']} | {r['id']} | {r['gold']} | {r['effective_decision']} | "
                         f"{r['fail_reason']} |")
    else:
        lines += ["", "（无兜底样本：三层校验 100% 通过）"]

    lines += ["", "## 6. 拍板结论", "", make_verdict(stats), "",
              "> ⚠️ 本节为机器建议，最终拍板与决策记录（Dxx）由人工写入 docs/决策记录.md。"]
    report_path = REPORT_DIR / f"report_{ts}_api_{slug}_{stem}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result_path, report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="E7 判定器 spike（OpenAI 兼容 API 多后端）")
    parser.add_argument("--model", type=str, default=None, help="模型名（默认 settings.LLM_MODEL）")
    parser.add_argument("--base-url", type=str, default=None, help="API 端点（默认 settings.LLM_BASE_URL）")
    parser.add_argument("--key", choices=["llm", "embedding"], default="llm",
                        help="用哪把 key：llm=LLM_API_KEY / embedding=EMBEDDING_API_KEY（硅基流动）")
    parser.add_argument("--enable-thinking", choices=["auto", "on", "off"], default="auto",
                        help="extra_body.enable_thinking（Qwen3 系防思考链拖慢；auto=不传）")
    parser.add_argument("--dataset", type=str, default=str(DEFAULT_DATASET),
                        help="数据集 jsonl 路径（默认 dataset_v2.jsonl）")
    parser.add_argument("--limit", type=int, default=0, help="每题只跑前 N 条（0=全量）")
    parser.add_argument("--no-json-mode", action="store_true", help="关闭 response_format")
    parser.add_argument("--no-report", action="store_true", help="冒烟模式：不落报告文件")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    model = args.model or settings.LLM_MODEL
    base_url = args.base_url or settings.LLM_BASE_URL
    api_key = settings.LLM_API_KEY if args.key == "llm" else (settings.EMBEDDING_API_KEY or "")
    if not api_key:
        raise SystemExit(f"--key {args.key} 对应的 key 未配置")
    extra_body = {"enable_thinking": args.enable_thinking == "on"} \
        if args.enable_thinking in ("on", "off") else None

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
    json_mode = not args.no_json_mode
    print(f"== E7 spike（API 后端）：{len(samples)} 条 / model={model} / base={base_url} / "
          f"json_mode={json_mode} / extra_body={extra_body or '无'} / dataset={dataset_hash} ==")

    client = OpenAI(api_key=api_key, base_url=base_url)
    records = run(client, model, samples, json_mode, extra_body)
    stats = summarize(records)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("\n== 汇总 ==")
    for k, s in stats.items():
        print(f"{k:11s} acc={s['accuracy']} eff_acc={s['effective_accuracy']} "
              f"parse={s['parse_success_rate']} p50={s['latency_p50_ms']}ms p95={s['latency_p95_ms']}ms "
              f"avg={s['avg_latency_ms']}ms tok_out={s['avg_completion_tokens']}")
    print(f"\n机器建议：{make_verdict(stats).splitlines()[0]}")

    if args.no_report:
        print("（--no-report：未落盘）")
        return
    result_path, report_path = write_outputs(records, stats, ts, dataset_path, dataset_hash,
                                             model, base_url, json_mode, extra_body)
    print(f"产物：\n  {result_path}\n  {report_path}")


if __name__ == "__main__":
    main()
