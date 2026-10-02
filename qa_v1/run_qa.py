# qa_v1/run_qa.py
"""qa_v1 实测执行器（只测不改：不 import 之外零干预，全部写盘落在 records/<run_id>/）。

用法（在 AI-Customer-Service 下，用仓库 venv 的 python）：
    python qa_v1\\run_qa.py --sections 1          # 段1（A~H，S1 记忆正确性组）
    python qa_v1\\run_qa.py --sections 2          # 段2（I~K，连发压力组）
    python qa_v1\\run_qa.py --sections 3          # 段3（L~O，鲁棒/隔离/污染组）
    python qa_v1\\run_qa.py --batches A,B         # 只跑指定批次（冒烟/调试）
    python qa_v1\\run_qa.py --run-id run_xxx      # 续跑已有 run（复用同一测试库与记录目录）

环境隔离（import app 之前设置，见 README §3）：
    DATABASE_URL / TELEMETRY_DIR / EXTRACT_JOURNAL_PATH / EVICT_LOG_PATH /
    EDIT_LOG_PATH / REDIS_DB=1 / QDRANT_COLLECTION=qa_v1_cards
主库、主 journal、主 telemetry、主 Redis 库、主 Qdrant 集合零接触（启动断言防呆）。
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime

# ============================================================
# 0. 路径与参数（先于任何 app import）
# ============================================================
HERE = os.path.dirname(os.path.abspath(__file__))          # qa_v1/
PROJECT_ROOT = os.path.dirname(HERE)                       # AI-Customer-Service/
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

SECTION_BATCHES = {"1": "ABCDEFGH", "2": "IJK", "3": "LMNO"}

parser = argparse.ArgumentParser(description="qa_v1 实测执行器")
parser.add_argument("--sections", default="", help="段选择，逗号分隔（1,2,3）")
parser.add_argument("--batches", default="", help="批次选择（逗号分隔，如 A,B），优先于 sections")
parser.add_argument("--run-id", default="", help="运行 id（默认按时间新建；指定已存在的 id = 续跑）")
args = parser.parse_args()

run_id = args.run_id or f"run_{datetime.now():%Y%m%d_%H%M%S}"
if args.batches:
    batch_filter = set(args.batches.split(","))
elif args.sections:
    batch_filter = set("".join(SECTION_BATCHES[s.strip()] for s in args.sections.split(",") if s.strip()))
else:
    batch_filter = set("".join(SECTION_BATCHES.values()))

REC_DIR = os.path.join(HERE, "records", run_id)
DB_DIR = os.path.join(PROJECT_ROOT, "data", "qa_v1")   # 必须与 DATABASE_URL 相对路径 ./data/qa_v1/ 解析结果一致
QA_DB = os.path.join(DB_DIR, f"app_{run_id}.db")
SRC_DB = os.path.join(PROJECT_ROOT, "data", "app.db")
os.makedirs(os.path.join(REC_DIR, "telemetry"), exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)

resume = os.path.exists(QA_DB)
if not resume:
    if not os.path.exists(SRC_DB):
        raise SystemExit(f"源库不存在：{SRC_DB}（无法建立测试副本）")
    shutil.copy2(SRC_DB, QA_DB)

# 日志（脚本自身；app 的 loguru 会另写 logs/）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(os.path.join(REC_DIR, "run.log"), encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("qa_v1")

# ============================================================
# 1. 环境隔离（import app 之前）
# ============================================================
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./data/qa_v1/app_{run_id}.db"
os.environ["TELEMETRY_DIR"] = os.path.join(REC_DIR, "telemetry")
os.environ["EXTRACT_JOURNAL_PATH"] = os.path.join(REC_DIR, "journal.log")
os.environ["EVICT_LOG_PATH"] = os.path.join(REC_DIR, "evict_log.jsonl")
os.environ["EDIT_LOG_PATH"] = os.path.join(REC_DIR, "edit_log.jsonl")
os.environ["REDIS_DB"] = "1"
os.environ["QDRANT_COLLECTION"] = "qa_v1_cards"

from app.core.config import settings                     # noqa: E402
from app.core.database import AsyncSessionLocal, init_db  # noqa: E402
from app.services.long_term_memory import (              # noqa: E402
    consolidator, evictor, journal, pool, store)
from app.api.v1.endpoints.chat import chat_endpoint      # noqa: E402
from app.models.schemas import ChatRequest               # noqa: E402

# —— 隔离防呆断言（绝不连到生产资源）——
assert "qa_v1" in settings.DATABASE_URL and run_id in settings.DATABASE_URL, settings.DATABASE_URL
assert run_id in settings.EXTRACT_JOURNAL_PATH, settings.EXTRACT_JOURNAL_PATH
assert run_id in settings.TELEMETRY_DIR, settings.TELEMETRY_DIR
assert settings.REDIS_DB == 1, settings.REDIS_DB
assert settings.QDRANT_COLLECTION == "qa_v1_cards", settings.QDRANT_COLLECTION
# 路径一致性防呆：DATABASE_URL 相对路径解析结果必须 == 脚本管理的 QA_DB 文件
# （曾因 qa_v1/data 与 data/qa_v1 双目录差异，导致 app 实际连到新建空库）
_url_path = os.path.abspath(settings.DATABASE_URL.split("///", 1)[1])
assert os.path.normcase(_url_path) == os.path.normcase(os.path.abspath(QA_DB)), \
    f"DATABASE_URL 解析路径({_url_path}) != QA_DB({os.path.abspath(QA_DB)})，拒绝启动"

TIMEOUT_TEXT = "抱歉，我思考得有点久了，请再说一遍试试？"
FAULT_TEXT = "抱歉，我这边出了点故障，请稍后再试。"
SEND_LIMIT_S = 90.0          # 客户端单条总时限（服务端 30s 超时 + SSE 推流余量）
GROUP_STAGGER_S = 0.4        # 连发组内错开间隔（模拟快速手打）
TURN_GAP_S = 1.5             # 单发之间的自然间隔

# ============================================================
# 2. 小工具（SQLite / journal 读取；只读，短连接）
# ============================================================

def sql_query(sql: str, params=()):
    """只读查询（短连接；database is locked 时返回 None 由调用方重试）。"""
    try:
        con = sqlite3.connect(QA_DB, timeout=8)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        log.warning(f"sql_query 失败（下轮重试）: {e}")
        return None


def db_max_message_id() -> int:
    rows = sql_query("SELECT COALESCE(MAX(id), 0) AS m FROM messages")
    return int(rows[0]["m"]) if rows else -1


def journal_watermark() -> int:
    return journal.current()


def cards_matching(patterns):
    """返回与任一 pattern 匹配的卡（content 或 entities LIKE）；用于等待锚点。"""
    if not patterns:
        return []
    where = " OR ".join(["(content LIKE ? OR IFNULL(entities,'') LIKE ?)"] * len(patterns))
    params = []
    for p in patterns:
        params.extend([f"%{p}%", f"%{p}%"])
    rows = sql_query(f"SELECT id, kind, subtype, status, source, entities, "
                     f"substr(content,1,60) AS preview "
                     f"FROM cards WHERE {where} ORDER BY id", tuple(params))
    return rows or []


def db_counts():
    rows = sql_query("SELECT (SELECT COUNT(*) FROM cards) AS cards, "
                     "(SELECT COUNT(*) FROM messages) AS messages, "
                     "(SELECT COALESCE(MAX(id),0) FROM messages) AS max_msg_id, "
                     "(SELECT COUNT(*) FROM edges) AS edges")
    return rows[0] if rows else {}


def snapshot(tag: str):
    """测试库快照：卡表全量 + 各 qa 会话消息概览 + 计数。"""
    snap = {
        "tag": tag, "ts": datetime.now().isoformat(),
        "counts": db_counts(),
        "cards": sql_query("SELECT * FROM cards ORDER BY id") or [],
        "qa_messages": sql_query(
            "SELECT id, session_id, role, substr(content,1,100) AS preview, created_at "
            "FROM messages WHERE session_id LIKE 'qa-v1-%' ORDER BY id") or [],
    }
    path = os.path.join(REC_DIR, f"db_{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    log.info(f"[snapshot] db_{tag}.json：卡 {snap['counts'].get('cards')}，"
             f"消息 {snap['counts'].get('messages')}，qa 消息 {len(snap['qa_messages'])}")
    return snap


# ============================================================
# 3. 启动序列（复刻 main.lifespan 最小集；不启用空闲收口巡检）
# ============================================================

async def bootstrap() -> float:
    t0 = time.perf_counter()
    await init_db()
    async with AsyncSessionLocal() as session:
        await store.startup_check(session)
        await pool.build(session)
    store.register_pool_hook(pool.on_store_write)
    try:
        async with AsyncSessionLocal() as session:
            await evictor.sweep(session)
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"启动淘汰巡检失败（不影响测试）: {e}")
    try:
        await consolidator.startup_scan()
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"写路径启动扫描失败（不影响测试）: {e}")
    elapsed = round(time.perf_counter() - t0, 1)
    log.info(f"[bootstrap] 启动序列完成，耗时 {elapsed}s；journal 水位={journal_watermark()}")
    return elapsed


def write_environment_md(boot_s, is_resume: bool):
    src_rows = sql_query("SELECT COUNT(*) AS c, COALESCE(MAX(id),0) AS m FROM messages")
    lines = [
        f"# 环境快照（{run_id}）", "",
        f"- 生成时间：{datetime.now().isoformat()}",
        f"- 续跑模式：{is_resume}",
        f"- 测试库：`{QA_DB}`（{'复用' if is_resume else '复制自 data/app.db'}）",
        f"- 测试库基线（时点副本）：messages {src_rows[0]['c']} 条 / max_id {src_rows[0]['m']}" if src_rows else "- 测试库基线：读取失败",
        f"- journal：`{settings.EXTRACT_JOURNAL_PATH}`（水位 {journal_watermark()}）",
        f"- telemetry：`{settings.TELEMETRY_DIR}`",
        f"- Redis DB：{settings.REDIS_DB}；Qdrant 集合：`{settings.QDRANT_COLLECTION}`",
        f"- PRESET：{settings.PRESET}；snapshot_hash：{settings.snapshot_hash()}",
        f"- 隔离断言：全部通过（DATABASE_URL / journal / telemetry / REDIS_DB / QDRANT_COLLECTION）",
        f"- 启动序列耗时：{boot_s}s",
        "",
        "## 配置快照（settings.experiment_snapshot）",
        "```json",
        json.dumps(settings.experiment_snapshot(), ensure_ascii=False, indent=1),
        "```",
    ]
    with open(os.path.join(REC_DIR, "environment.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ============================================================
# 4. 发送与等待
# ============================================================

async def send_one(case: dict, conv_fh, launch_order: int) -> dict:
    t0 = time.perf_counter()
    events, reply_parts, meta = [], [], None
    status = "ok"
    resp = None
    try:
        async def _consume():
            nonlocal meta, resp
            resp = await chat_endpoint(
                ChatRequest(session_id=case["session"], message=case["message"]), None)
            async for raw in resp.body_iterator:
                chunk = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                for line in chunk.splitlines():
                    if not line.startswith("data: "):
                        continue
                    evt = json.loads(line[6:])
                    events.append(evt.get("event"))
                    if evt.get("event") == "meta":
                        meta = evt.get("content")
                    elif evt.get("event") == "message":
                        reply_parts.append(str(evt.get("content") or ""))
        resp = await asyncio.wait_for(_consume(), timeout=SEND_LIMIT_S)
    except asyncio.TimeoutError:
        status = "client_timeout"
    except Exception as e:                                   # noqa: BLE001
        status = f"error:{type(e).__name__}"
        log.warning(f"[{case['id']}] 发送异常: {type(e).__name__}: {e}")
    finally:
        try:
            if resp is not None and hasattr(resp, "body_iterator") and hasattr(resp.body_iterator, "aclose"):
                await resp.body_iterator.aclose()
        except Exception:                                    # noqa: BLE001
            pass

    reply = "".join(reply_parts)
    if status == "ok":
        if reply == TIMEOUT_TEXT:
            status = "server_timeout"
        elif reply == FAULT_TEXT:
            status = "server_fault"
    rec = {
        "id": case["id"], "batch": case["batch"], "session": case["session"],
        "group": case.get("group"), "launch_order": launch_order,
        "message": case["message"], "status": status,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "reply_len": len(reply), "reply": reply, "meta": meta,
        "events": len(events),
        "sent_at": datetime.now().isoformat(),
    }
    conv_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    conv_fh.flush()
    mark = {"ok": "OK", "server_timeout": "TIMEOUT(超时兜底)", "client_timeout": "CLIENT_TIMEOUT",
            "server_fault": "FAULT(异常兜底)"}.get(status, status)
    meta_brief = "无"
    if meta:
        meta_brief = (f"注入{meta.get('count')}张" if meta.get("status") == "injected"
                      else str(meta.get("status")))
    log.info(f"[{case['id']}] {mark} {rec['elapsed_s']}s 回复{rec['reply_len']}字 meta={meta_brief}")
    return rec


async def run_group(cases: list, conv_fh, order_base: int) -> list:
    """连发组：各条错开 GROUP_STAGGER_S 后并发发送（真实模拟快速手打）。"""
    async def one(i, case):
        await asyncio.sleep(i * GROUP_STAGGER_S)
        return await send_one(case, conv_fh, order_base + i)
    return list(await asyncio.gather(*[one(i, c) for i, c in enumerate(cases)]))


async def wait_anchor(case: dict, waits_fh) -> dict:
    """等待锚点（超时不视为脚本错误：记录状态继续，作为证据）。"""
    t0 = time.perf_counter()
    kind = case["wait"]
    timeout = float(case.get("timeout", 240))
    result, detail = "timeout", {}
    if kind == "cards":
        patterns, mode = case["patterns"], case.get("mode", "any")
        while time.perf_counter() - t0 < timeout:
            matched = cards_matching(patterns)
            detail = {"matched": matched}
            def _hit(p):
                return any(p in (m["preview"] or "") or p in (m["entities"] or "") for m in matched)
            if mode == "all":
                if all(_hit(p) for p in patterns):
                    result = "ok"
                    break
            elif matched:
                result = "ok"
                break
            await asyncio.sleep(3)
    elif kind == "pending_below":
        threshold = int(case.get("threshold", 8))
        while time.perf_counter() - t0 < timeout:
            wm, mx = journal_watermark(), db_max_message_id()
            pending = max(0, mx - wm) if mx >= 0 else -1
            detail = {"watermark": wm, "max_msg_id": mx, "pending": pending}
            if detail["pending"] < threshold:
                result = "ok"
                break
            await asyncio.sleep(3)
    elif kind == "watermark":
        target = db_max_message_id()          # 调用时快照（不滚动）
        detail = {"target": target}
        while time.perf_counter() - t0 < timeout:
            wm = journal_watermark()
            detail.update({"watermark": wm, "target": target})
            if wm >= target:
                result = "ok"
                break
            await asyncio.sleep(3)
    elif kind == "stable":
        seconds = float(case.get("seconds", 20))
        last_state, last_change = None, time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            c = db_counts()
            state = (c.get("max_msg_id"), c.get("cards"))
            if state != last_state:
                last_state, last_change = state, time.perf_counter()
            detail = {"state": state, "stable_for_s": round(time.perf_counter() - last_change, 1)}
            if time.perf_counter() - last_change >= seconds:
                result = "ok"
                break
            await asyncio.sleep(3)
    waited = round(time.perf_counter() - t0, 1)
    rec = {"id": case["id"], "batch": case["batch"], "wait": kind, "result": result,
           "waited_s": waited, "detail": detail,
           "final": db_counts() if kind != "cards" else {"matched": len(detail.get("matched") or [])}}
    waits_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    waits_fh.flush()
    log.info(f"[{case['id']}] wait:{kind} -> {result}（{waited}s）{str(detail)[:160]}")
    return rec


# ============================================================
# 5. 主流程
# ============================================================

async def main():
    log.info(f"===== qa_v1 实测开始 run_id={run_id} 批次={'/'.join(sorted(batch_filter))} "
             f"resume={resume} =====")
    boot_s = await bootstrap()
    write_environment_md(boot_s, resume)
    before = snapshot("before")
    snap_before_ids = set(c["id"] for c in before["cards"])

    with open(os.path.join(HERE, "cases.jsonl"), encoding="utf-8") as f:
        all_cases = [json.loads(line) for line in f if line.strip()]
    cases = [c for c in all_cases if c["batch"] in batch_filter]
    log.info(f"载入用例 {len(cases)} 条（全量 {len(all_cases)}）")

    conv_path = os.path.join(REC_DIR, "conversation.jsonl")
    waits_path = os.path.join(REC_DIR, "waits.jsonl")
    results, waits = [], []
    with open(conv_path, "a", encoding="utf-8") as conv_fh, \
         open(waits_path, "a", encoding="utf-8") as waits_fh:
        i, order = 0, 0
        while i < len(cases):
            case = cases[i]
            if case["type"] == "send":
                group = case.get("group")
                if group:
                    grp, j = [], i
                    while j < len(cases) and cases[j]["type"] == "send" and cases[j].get("group") == group:
                        grp.append(cases[j])
                        j += 1
                    log.info(f"----- 连发组 {group}：{len(grp)} 条（错开 {GROUP_STAGGER_S}s）-----")
                    results.extend(await run_group(grp, conv_fh, order))
                    order += len(grp)
                    i = j
                else:
                    log.info(f"----- [{case['id']}] 单发 -----")
                    results.append(await send_one(case, conv_fh, order))
                    order += 1
                    i += 1
                    await asyncio.sleep(TURN_GAP_S)
            elif case["type"] == "wait":
                waits.append(await wait_anchor(case, waits_fh))
                i += 1
            else:
                log.warning(f"未知用例类型：{case}")
                i += 1

    log.info("主流程结束：等写路径后台任务收尾（wait_idle）…")
    try:
        await consolidator.wait_idle(timeout=120.0)
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"wait_idle 异常（忽略）: {e}")
    try:
        await asyncio.sleep(2)
        await consolidator.wait_idle(timeout=60.0)
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"wait_idle 二次等待异常（忽略）: {e}")
    after = snapshot("after")

    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    summary = {
        "run_id": run_id, "sections": sorted(batch_filter),
        "sends": len(results), "by_status": by_status,
        "waits": [{"id": w["id"], "result": w["result"], "waited_s": w["waited_s"]} for w in waits],
        "cards_before": len(snap_before_ids), "cards_after": len(after["cards"]),
        "card_ids_added": sorted(set(c["id"] for c in after["cards"]) - snap_before_ids),
        "qa_messages_after": len(after["qa_messages"]),
        "journal_watermark_final": journal_watermark(),
        "db_max_msg_id_final": db_max_message_id(),
    }
    with open(os.path.join(REC_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    log.info("===== 汇总 =====")
    log.info(f"发送 {summary['sends']} 条：{by_status}")
    log.info(f"等待锚点 {len(waits)} 个：{[(w['id'], w['result']) for w in waits]}")
    log.info(f"卡片 {summary['cards_before']} -> {summary['cards_after']}（新增 {summary['card_ids_added']}）")
    log.info(f"journal 水位={summary['journal_watermark_final']} / DB max_id={summary['db_max_msg_id_final']}")
    log.info(f"===== 结束。记录在 {REC_DIR} =====")


if __name__ == "__main__":
    asyncio.run(main())
