"""全量跑：一个条件 × 一个样本的所有 QA，逐题落 JSONL（断点可续）。

三个条件族：
- sut            ：完整记忆链路（gate → 检索 → 精筛 → 注入 → 生成）
- win{N}         ：纯窗口对照（只给最近 N 条对话原文，无记忆）
- rag（后续消融）：关掉某模块的变体

窗口口径统一：所有条件在提问前都把「对话最后 W 条原文」放进上下文，
sut 的 W = WINDOW_SIZE(10)，与 win10 完全对齐——两者之差就是记忆系统的净贡献。

用法：
    python run_eval.py --sample 0 --cond s0 --mode sut
    python run_eval.py --sample 0 --cond w10 --mode win10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import bench_lib as B


# generation_service.generate_reply 的兜底文案标记：出现即代表这一轮的 LLM 调用挂了
# （额度不足/网络/限流），答案不可用——必须作废重跑，绝不能当成"系统答错了"计分。
_FALLBACK_MARKERS = ("走神", "崩溃原因")


_DATE_FORMAT = "%I:%M %p on %d %B, %Y"   # LoCoMo: "1:56 pm on 8 May, 2023"


def _date_stamp(raw: str | None) -> str | None:
    try:
        from datetime import datetime
        return datetime.strptime((raw or "").strip(), _DATE_FORMAT).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None


def _logical_msgs(sample: dict, *, with_dates: bool = False) -> list[dict]:
    """把对话摊平成逐条消息。

    with_dates（全上下文公平版）：每条前加会话日期。不加日期的话，全上下文读到的
    只有 "last week" 这类相对说法，时间题上会被我们的绝对日期碾压——那是信息差异，
    不是检索能力差异，用来当基线是稻草人。
    """
    out = []
    for m in B.flatten_conversation(sample):
        text = m["content"]
        if with_dates:
            stamp = _date_stamp(m.get("session_date"))
            if stamp:
                text = f"[{stamp}] {text}"
        out.append({"role": "user", "content": text})
    return out


async def ask_sut(question: str, logical: list[dict], window: int, tag: str) -> dict:
    """走完整记忆链路（chat_pipeline.handle_message = 线上同一条路）。"""
    from app.core import short_term_memory as hot
    from app.models.schemas import ChatMessage
    from app.services import chat_pipeline

    sid = f"eval_{tag}"
    await hot.redis_client.delete(hot._key(sid))
    for m in logical[-window:] if window > 0 else []:
        await hot.redis_client.rpush(
            hot._key(sid), ChatMessage(role=m["role"], content=m["content"]).model_dump_json())

    B.reset_capture()
    t0 = time.perf_counter()
    outcome = await chat_pipeline.handle_message(sid, question)
    dt = time.perf_counter() - t0
    await hot.redis_client.delete(hot._key(sid))
    cap = B.capture()

    injected_text = cap.memory_text or ""
    return {
        "reply": outcome.reply,
        "elapsed_s": round(dt, 3),
        "triage": (cap.retrieval_trace or {}).get("triage"),
        "gate_decision": ((cap.retrieval_trace or {}).get("gate") or {}).get("decision"),
        "gate_origin": ((cap.retrieval_trace or {}).get("gate") or {}).get("origin"),
        "candidates": (cap.retrieval_trace or {}).get("merged_count"),
        "judged": (cap.rerank_trace or {}).get("judged"),
        "tiers": (cap.rerank_trace or {}).get("tiers"),
        "injected_ids": cap.injector_trace.get("injected"),
        "injected_text": injected_text,
        "context_for_hit": injected_text + "\n" + "\n".join(m["content"] for m in logical[-window:]),
        "tokens": {"prompt": cap.prompt_tokens, "completion": cap.completion_tokens,
                   "total": cap.total_tokens},
        "memory_meta": outcome.memory_meta,
    }


async def ask_window(question: str, logical: list[dict], window: int,
                     *, collapse: bool = False) -> dict:
    """纯窗口对照：无记忆注入，只把最近 N 条原文拼进 prompt（同一 system prompt / 同一模型）。

    collapse=True 用于全上下文：把整段对话合成一条 user 消息。419 条独立消息会带来
    每条几 token 的 JSON 开销，也不像真实「把对话记录贴进去」的用法。
    """
    from app.core import short_term_memory as hot
    from app.models.schemas import ChatMessage
    from app.services import generation_service

    if collapse:
        win = [ChatMessage(role="user",
                           content="\n".join(m["content"] for m in logical[-window:]))]
    else:
        win = [ChatMessage(role=m["role"], content=m["content"]) for m in logical[-window:]]
    package = hot.assemble_package(current_input=question, window=win, memory_context=None)
    t0 = time.perf_counter()
    gen = await generation_service.generate_reply(package)
    dt = time.perf_counter() - t0
    return {
        "reply": gen.reply,
        "elapsed_s": round(dt, 3),
        "triage": None, "gate_decision": None, "gate_origin": None,
        "candidates": None, "judged": None, "tiers": None,
        "injected_ids": [], "injected_text": "",
        "context_for_hit": "\n".join(m["content"] for m in logical[-window:]),
        "tokens": {"prompt": gen.prompt_tokens, "completion": gen.completion_tokens,
                   "total": gen.total_tokens},
        "memory_meta": None,
    }


async def main_async(args) -> None:
    mode = args.mode
    if mode == "sut":
        window = args.window
        out_path = B.OUT_DIR / f"res_{args.cond}.jsonl"
    elif mode.startswith("win"):
        window = int(mode[3:])
        out_path = B.OUT_DIR / f"res_{args.cond}.jsonl"
    elif mode in ("full", "fulld"):
        window = 0                      # 待 logical 加载后替换成总条数
        out_path = B.OUT_DIR / f"res_{args.cond}.jsonl"
    else:
        raise SystemExit(f"未知 mode: {mode}")

    B.prepare_env(args.cond, window_size=args.window, state=args.state)
    B.apply_language_patches()

    if mode == "sut":
        from app.core.database import AsyncSessionLocal, init_db
        from app.services.long_term_memory import pool, store

        B.install_spies()
        B.enable_writeoff()
        await init_db()
        async with AsyncSessionLocal() as session:   # 与 main.py lifespan 同序
            await store.startup_check(session)       # 一致性检查 + Qdrant 对账（补齐缺向量）
            await pool.build(session)
        store.register_pool_hook(pool.on_store_write)
        from app.services.long_term_memory import evictor
        try:
            async with AsyncSessionLocal() as session:
                await evictor.sweep(session)         # 启动淘汰巡检（本样本卡片无常过期时间→空转）
        except Exception as exc:  # noqa: BLE001
            print(f"[sut] 淘汰巡检失败（不影响评测）: {exc}", flush=True)
        async with AsyncSessionLocal() as session:
            cards = await store.list_cards(session, status=None, limit=None)
        print(f"[sut] 库内卡片 {len(cards)} 张（active "
              f"{sum(1 for c in cards if c.status == 'active')}）；窗口 {window}", flush=True)

    sample = B.load_locomo()[args.sample]
    full_ctx = mode in ("full", "fulld")
    logical = _logical_msgs(sample, with_dates=(mode == "fulld"))
    if full_ctx:
        window = len(logical)
        print(f"[{mode}] 全上下文：{len(logical)} 条消息整体放入，日期前缀={'有' if mode == 'fulld' else '无'}", flush=True)
    qas = B.qa_list(sample)
    if args.qids:
        qas = [q for q in qas if q["qid"] in set(args.qids)]
    elif args.limit:
        qas = qas[:args.limit]

    done: set[int] = set()
    if out_path.exists() and not args.fresh:
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                    if not rec.get("error"):     # 失败行不算完成，下次续跑重做
                        done.add(rec["qid"])
                except Exception:  # noqa: BLE001
                    pass
        print(f"续跑：已完成 {len(done)} 题", flush=True)

    t_all = time.perf_counter()
    with open(out_path, "a", encoding="utf-8") as f:
        for i, item in enumerate(qas, 1):
            if item["qid"] in done:
                continue
            gold = B.gold_answer(item) if not item["is_adversarial"] else item.get("adversarial_answer", "")
            ev = B.evidence_texts(sample, item.get("evidence", []))
            t0 = time.perf_counter()
            try:
                if mode == "sut":
                    res = await ask_sut(item["question"], logical, window,
                                        tag=f"{args.cond}_{item['qid']}")
                else:
                    res = await ask_window(item["question"], logical, window,
                                           collapse=full_ctx)
                err = None
                if any(m in (res.get("reply") or "") for m in _FALLBACK_MARKERS):
                    err = "生成兜底文案（LLM 调用失败：额度/网络/限流），该题作废待重跑"
            except Exception as exc:  # noqa: BLE001 —— 单题失败不炸整轮
                res, err = {}, f"{type(exc).__name__}: {exc}"
            rec = {
                "qid": item["qid"],
                "category": item.get("category"),
                "is_adversarial": item["is_adversarial"],
                "question": item["question"],
                "gold": gold,
                "evidence": item.get("evidence", []),
                "evidence_texts": ev,
                "cond": args.cond,
                "mode": mode,
                "window": window,
                "error": err,
                "wall_s": round(time.perf_counter() - t0, 3),
                **res,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if i % 10 == 0 or i == len(qas):
                print(f"  [{i}/{len(qas)}] {time.perf_counter() - t_all:.0f}s "
                      f"last={rec.get('reply', '')[:60]!r}", flush=True)

    print(f"完成 → {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--cond", required=True)
    ap.add_argument("--state", default=None, help="复用哪套库/向量集合（默认同 cond）")
    ap.add_argument("--mode", required=True)
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--qids", type=int, nargs="+")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--fresh", action="store_true", help="丢弃已有结果重跑")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
