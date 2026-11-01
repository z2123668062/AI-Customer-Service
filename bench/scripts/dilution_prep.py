"""稀释实验的素材准备：有效块 + 噪声流 + 题目集。

设计（定死，不许事后调）：
- 有效块：sample 0 的第 2 到 151 条（150 条），约 5700 token。这是"全部历史"。
- 题目：证据完整落在块内的 65 道正常题。
- 噪声：另一个样本的对话，时间戳接在块之后，模拟"历史继续增长但与问题无关"。
- 稀释档位：噪声条数 0 / 50 / 100 / 150 / 300 / 450，
  对应全上下文窗口（固定为 150 条）里被挤出的比例 0% / 25% / 40% / 50% / 67% / 75%。

为什么噪声要接在后面而不是插进去：窗口取"最后 N 条"，追加噪声就自然会按 N/150
线性挤出有效内容，不需要打乱时间顺序，也不会破坏有效块的会话结构。

输出：out/dilution_plan.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

import bench_lib as B

BLOCK_START = 2
BLOCK_LEN = 150
NOISE_SAMPLE = 3          # 另一段对话，说话人不同、话题相近，作为"无关历史"
LEVELS = (0, 25, 50, 75, 100, 150, 300, 450)


def main() -> None:
    data = B.load_locomo()
    flat = B.flatten_conversation(data[0])
    pos = {m["dia_id"]: i for i, m in enumerate(flat) if m.get("dia_id")}

    block = flat[BLOCK_START: BLOCK_START + BLOCK_LEN]
    block_ids = {id(m) for m in block}

    qas = [q for q in B.qa_list(data[0]) if not q["is_adversarial"]]
    picked = []
    for q in qas:
        idx = [pos[e] for e in q.get("evidence", []) if e in pos]
        if not idx:
            continue
        if min(idx) >= BLOCK_START and max(idx) < BLOCK_START + BLOCK_LEN:
            picked.append({"qid": q["qid"], "question": q["question"],
                           "gold": B.gold_answer(q), "category": q["category"],
                           "evidence": q.get("evidence", [])})
    _ = block_ids

    # 噪声：另一段对话的前 450 条，时间戳排在有效块之后（每天推进一段）
    noise_src = B.flatten_conversation(data[NOISE_SAMPLE])
    last_date = None
    for m in block:
        stamped = m.get("session_date")
        if stamped:
            try:
                last_date = datetime.strptime(stamped.strip(), "%I:%M %p on %d %B, %Y")
            except Exception:  # noqa: BLE001
                pass
    base = (last_date or datetime(2023, 1, 1)) + timedelta(days=1)
    noise = []
    for i, m in enumerate(noise_src[:max(LEVELS)]):
        noise.append({
            "content": m["content"],
            "created_at": (base + timedelta(hours=i * 6)).strftime("%Y-%m-%d %H:%M:%S"),
            "origin": f"noise_s{NOISE_SAMPLE}",
        })

    block_msgs = [{"content": m["content"],
                   "created_at": (m.get("session_date") and _to_iso(m["session_date"]))
                                 or "2023-01-01 00:00:00",
                   "origin": "valid"}
                  for m in block]

    plan = {
        "block_start": BLOCK_START,
        "block_len": BLOCK_LEN,
        "block_tokens_est": sum(len(m["content"]) for m in block) // 4,
        "noise_sample": NOISE_SAMPLE,
        "levels": list(LEVELS),
        "questions": picked,
        "block": block_msgs,
        "noise": noise,
    }
    out = B.OUT_DIR / "dilution_plan.json"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"有效块 {len(block_msgs)} 条，约 {plan['block_tokens_est']} token")
    print(f"题目 {len(picked)} 道：{sorted(q['qid'] for q in picked)}")
    print(f"噪声池 {len(noise)} 条（源 sample {NOISE_SAMPLE}）")
    print(f"档位 {LEVELS} → 全上下文窗口（150 条）挤出比例 "
          f"{[round(min(n / BLOCK_LEN, 1) * 100) for n in LEVELS]}%")
    print(f"已保存 {out}")


def _to_iso(raw: str) -> str:
    try:
        return datetime.strptime((raw or "").strip(), "%I:%M %p on %d %B, %Y").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return "2023-01-01 00:00:00"


if __name__ == "__main__":
    sys.exit(main())
