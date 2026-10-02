# -*- coding: utf-8 -*-
"""E7 判定器 spike runner：deepseek-flash 中文判断题实测。

设计与流程权威文档：docs/判定器Spike文档.md
测试三道判断题：page_fault（缺页是非）/ supersede（取代三选一）/ backfill（回填是非）。

用法（cwd 任意，脚本自行定位项目根）：
    & "E:\\毕设_记忆系统\\.venv\\Scripts\\python.exe" -X utf8 -m evaluation.spikes.e7_gate.run_spike
    ... --limit 3        # 快速冒烟：每题只跑前 N 条
    ... --no-json-mode   # 关闭 response_format 的对照组

产物：evaluation/reports/e7_gate_spike/result_<ts>.json + report_<ts>.md
纪律：本 spike 刻意不做重试，测的是"裸结构化稳定性"（见设计文档 §3）。
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

# ---- 项目根定位（本文件: AI-Customer-Service/evaluation/spikes/e7_gate/run_spike.py）----
BASE_DIR = Path(__file__).resolve().parents[3]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from openai import OpenAI  # noqa: E402

from app.core.config import settings  # noqa: E402

DATASET_PATH = Path(__file__).resolve().parent / "dataset.jsonl"
REPORT_DIR = BASE_DIR / "evaluation" / "reports" / "e7_gate_spike"

# ============ 三道判断题的判定模式（三闸门之"输入约束"）============
# 纪律：每题一套极简 prompt、枚举写死、few-shot 含边界/反例、要求仅输出 json。
# json_object 模式要求 prompt 中出现 "json" 字样，故格式说明统一用 "json 对象"。

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
}


# ============ 三闸门之"结构化校验"（格式 → schema → 语义）============
def _strip_fence(text: str) -> str:
    """剥离常见 markdown 代码围栏（生产实现同样需要这层宽容）。"""
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t[3:]
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3].rstrip()
    return t


def three_layer_check(task_key: str, raw: str) -> tuple[dict | None, str | None]:
    """三层校验：格式层 → schema 层 → 语义层。返回 (parsed, None) 或 (None, 失败原因)。"""
    # 格式层：能否解析为 json
    try:
        obj = json.loads(_strip_fence(raw))
    except Exception as e:  # noqa: BLE001 - 任何解析失败都归格式层
        return None, f"format: JSON 解析失败({e})"
    if not isinstance(obj, dict):
        return None, "format: 顶层不是 json 对象"
    # schema 层：字段齐全 + 枚举合法 + confidence 可转数字
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
    # 语义层：值域合理
    if not (0.0 <= conf <= 1.0):
        return None, f"semantic: confidence 越界 {conf}"
    if not isinstance(obj["reason"], str) or not obj["reason"].strip():
        return None, "semantic: reason 为空"
    return {"decision": decision.strip().lower(), "confidence": conf,
            "reason": obj["reason"].strip()}, None


# ============ 调用与主循环 ============
def call_judge(client: OpenAI, task_key: str, sample: dict, json_mode: bool):
    """单次判定调用（spike 不重试）。返回 (raw_text, latency_ms, usage)。"""
    spec = TASKS[task_key]
    messages = [
        {"role": "system", "content": spec["system"]},
        {"role": "user", "content": spec["user_template"].format(**sample["input"])},
    ]
    kwargs = dict(model=settings.LLM_MODEL, messages=messages, temperature=0.0, top_p=0.1)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    t0 = time.perf_counter()
    resp = client.chat.completions.create(**kwargs, timeout=30)
    latency_ms = (time.perf_counter() - t0) * 1000
    return resp.choices[0].message.content or "", latency_ms, resp.usage


def run(samples: list[dict], json_mode: bool) -> list[dict]:
    client = OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
    records: list[dict] = []
    total = len(samples)
    for i, sample in enumerate(samples, 1):
        task_key, spec = sample["task"], TASKS[sample["task"]]
        rec: dict = {"task": task_key, "id": sample["id"], "gold": sample["gold"], "note": sample.get("note", "")}
        try:
            raw, latency_ms, usage = call_judge(client, task_key, sample, json_mode)
            rec["latency_ms"] = round(latency_ms, 1)
            rec["raw"] = raw[:500]
            if usage is not None:
                rec["prompt_tokens"] = usage.prompt_tokens
                rec["completion_tokens"] = usage.completion_tokens
            parsed, fail_reason = three_layer_check(task_key, raw)
        except Exception as e:  # noqa: BLE001 - 调用失败也计入 fail-safe
            parsed, fail_reason = None, f"call: {type(e).__name__}: {e}"
            rec["raw"] = ""
        if parsed is None:
            rec.update(valid=False, fail_reason=fail_reason, decision=None, confidence=None, reason="")
            # 失败样本按 fail-safe 默认方向计算"系统最终行为"
            rec["effective_decision"] = spec["failsafe"]
        else:
            rec.update(valid=True, fail_reason=None, decision=parsed["decision"],
                       confidence=parsed["confidence"], reason=parsed["reason"])
            rec["effective_decision"] = parsed["decision"]
        rec["correct"] = (rec["decision"] == sample["gold"]) if rec["valid"] else None
        rec["effective_correct"] = (rec["effective_decision"] == sample["gold"])
        flag = "✓" if rec["effective_correct"] else "✗"
        mark = "" if rec["valid"] else " [fail-safe]"
        print(f"[{i}/{total}] {task_key:11s} {sample['id']} {flag}{mark}"
              + (f"  {rec.get('fail_reason')}" if not rec["valid"] else ""))
        records.append(rec)
    return records


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
    return round(sum(values) / len(values), 1) if values else None


def summarize(records: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for task_key in TASKS:
        rs = [r for r in records if r["task"] == task_key]
        if not rs:
            continue
        valid = [r for r in rs if r["valid"]]
        stats[task_key] = {
            "label": TASKS[task_key]["label"],
            "n": len(rs),
            "valid_n": len(valid),
            "accuracy": round(sum(1 for r in valid if r["correct"]) / len(valid), 3) if valid else None,
            "effective_accuracy": round(sum(1 for r in rs if r["effective_correct"]) / len(rs), 3),
            "parse_success_rate": round(len(valid) / len(rs), 3),
            "failsafe_rate": round(1 - len(valid) / len(rs), 3),
            "latency_p50_ms": _percentile([r["latency_ms"] for r in rs if "latency_ms" in r], 50),
            "latency_p95_ms": _percentile([r["latency_ms"] for r in rs if "latency_ms" in r], 95),
            "avg_prompt_tokens": _mean([r["prompt_tokens"] for r in rs if "prompt_tokens" in r]),
            "avg_completion_tokens": _mean([r["completion_tokens"] for r in rs if "completion_tokens" in r]),
        }
        if task_key == "page_fault":
            stats[task_key]["dangerous_miss"] = sum(
                1 for r in rs if r["effective_decision"] == "no" and r["gold"] == "yes")
            stats[task_key]["safe_extra"] = sum(
                1 for r in rs if r["effective_decision"] == "yes" and r["gold"] == "no")
    return stats


def make_verdict(stats: dict[str, dict]) -> str:
    """按 docs/判定器Spike文档.md §4 拍板规则给机器建议（人工最终拍板）。"""
    accs = {k: s["accuracy"] for k, s in stats.items() if s["accuracy"] is not None}
    min_acc = min(accs.values()) if accs else 0.0
    min_parse = min(s["parse_success_rate"] for s in stats.values())
    lines = []
    if min_acc >= 0.85 and min_parse >= 0.95:
        lines.append("**机器建议：直接采用** —— 三题 accuracy ≥ 0.85 且 parse_success_rate ≥ 0.95，"
                     "deepseek-flash 约束模式可直接作为 gate.py 判定后端（仍需人工核延迟/成本是否可接受）。")
    elif min_acc >= 0.80:
        lines.append("**机器建议：采用 + 加固** —— accuracy 达标但 parse_success_rate < 0.95，"
                     "gate.py 必须内建 1 次重试 / function calling 后使用。")
    elif min_acc >= 0.70:
        lines.append("**机器建议：部分降级** —— 存在单题 accuracy < 0.70（见下表最低项），"
                     "该判断点需补确定性前置或引入更强模型（deepseek-chat）升级档。")
    else:
        lines.append("**机器建议：整体降级** —— 多题 accuracy < 0.70，需重新评估判定后端选型。")
    low = [f"{k}={v}" for k, v in accs.items() if v < 0.85]
    if low:
        lines.append(f"未达冲刺线（0.85）的任务：{', '.join(low)}")
    return "\n".join(lines)


# ============ 报告落盘 ============
def write_outputs(records: list[dict], stats: dict[str, dict], ts: str,
                  json_mode: bool, dataset_hash: str) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "model": settings.LLM_MODEL,
        "base_url": settings.LLM_BASE_URL,
        "temperature": 0.0,
        "top_p": 0.1,
        "json_mode": json_mode,
        "dataset": str(DATASET_PATH.relative_to(BASE_DIR)),
        "dataset_sha256_12": dataset_hash,
        "n_samples": len(records),
        "run_at": ts,
        "retry": 0,
    }
    result_path = REPORT_DIR / f"result_{ts}.json"
    result_path.write_text(
        json.dumps({"meta": meta, "stats": stats, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 人读报告
    lines = [f"# E7 判定器 Spike 报告（{ts}）", "",
             "> 设计与流程：docs/判定器Spike文档.md ｜ 结论需人工最终拍板（见 §5）。", "",
             "## 1. 环境登记（实验条件）", "",
             f"- 模型：`{meta['model']}` @ `{meta['base_url']}`",
             f"- 判定模式：temperature={meta['temperature']} / top_p={meta['top_p']} / "
             f"json_mode={meta['json_mode']} / 重试={meta['retry']} 次",
             f"- 数据集：`{meta['dataset']}`（sha256前12位 `{dataset_hash}`，共 {meta['n_samples']} 条）",
             f"- 运行时间：{ts}", "",
             "## 2. 汇总指标", "",
             "| 任务 | n | accuracy | effective_accuracy | parse_success | failsafe | P50(ms) | P95(ms) | prompt tok | completion tok |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in stats.items():
        lines.append(f"| {s['label']} | {s['n']} | {s['accuracy']} | {s['effective_accuracy']} | "
                     f"{s['parse_success_rate']} | {s['failsafe_rate']} | {s['latency_p50_ms']} | "
                     f"{s['latency_p95_ms']} | {s['avg_prompt_tokens']} | {s['avg_completion_tokens']} |")

    pf = stats.get("page_fault", {})
    if pf:
        lines += ["", "## 3. 缺页判定误差方向分解（代价不对称的实证）", "",
                  f"- `dangerous_miss`（该回忆没回忆，漏判=失忆，最危险）：**{pf.get('dangerous_miss', 0)}**",
                  f"- `safe_extra`（不该回忆却回忆，多回源，有精筛兜底）：**{pf.get('safe_extra', 0)}**"]

    wrong = [r for r in records if r["valid"] and not r["correct"]]
    failsafe_rs = [r for r in records if not r["valid"]]
    lines += ["", "## 4. 错例与兜底明细", ""]
    if wrong:
        lines += ["### 4.1 判定错误（校验通过但判错）", "",
                  "| 任务 | id | gold | 判定 | confidence | reason |", "|---|---|---|---|---|---|"]
        for r in wrong:
            lines.append(f"| {r['task']} | {r['id']} | {r['gold']} | {r['decision']} | "
                         f"{r['confidence']} | {r['reason']} |")
    else:
        lines.append("（无判定错误）")
    if failsafe_rs:
        lines += ["", "### 4.2 fail-safe 兜底（三层校验未通过）", "",
                  "| 任务 | id | gold | 兜底方向 | 失败原因 |", "|---|---|---|---|---|"]
        for r in failsafe_rs:
            lines.append(f"| {r['task']} | {r['id']} | {r['gold']} | {r['effective_decision']} | "
                         f"{r['fail_reason']} |")
    else:
        lines += ["", "（无兜底样本：三层校验 100% 通过）"]

    lines += ["", "## 5. 拍板结论", "", make_verdict(stats), "",
              "> ⚠️ 本节为机器建议，最终拍板与决策记录（Dxx）由人工写入 docs/决策记录.md。"]
    report_path = REPORT_DIR / f"report_{ts}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result_path, report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="E7 判定器 spike（deepseek-flash 判断题实测）")
    parser.add_argument("--limit", type=int, default=0, help="每题只跑前 N 条（0=全量）")
    parser.add_argument("--no-json-mode", action="store_true", help="关闭 response_format 的对照组")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    samples = [json.loads(ln) for ln in DATASET_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.limit > 0:
        per_task: dict[str, int] = {}
        limited = []
        for s in samples:
            per_task[s["task"]] = per_task.get(s["task"], 0) + 1
            if per_task[s["task"]] <= args.limit:
                limited.append(s)
        samples = limited

    dataset_hash = hashlib.sha256(DATASET_PATH.read_bytes()).hexdigest()[:12]
    json_mode = not args.no_json_mode
    print(f"== E7 判定器 spike：{len(samples)} 条样本 / model={settings.LLM_MODEL} / "
          f"json_mode={json_mode} / dataset={dataset_hash} ==")

    records = run(samples, json_mode)
    stats = summarize(records)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path, report_path = write_outputs(records, stats, ts, json_mode, dataset_hash)

    print("\n== 汇总 ==")
    for k, s in stats.items():
        print(f"{k:11s} acc={s['accuracy']} eff_acc={s['effective_accuracy']} "
              f"parse={s['parse_success_rate']} p95={s['latency_p95_ms']}ms")
    print(f"\n机器建议：{make_verdict(stats).splitlines()[0]}")
    print(f"产物：\n  {result_path}\n  {report_path}")


if __name__ == "__main__":
    main()
