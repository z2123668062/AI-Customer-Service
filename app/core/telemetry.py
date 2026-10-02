"""埋点与回溯日志（E3 实装）：记忆系统实验数据的唯一来源。

职责：把每次请求的记忆全链路落盘为一条 JSONL 记录，三组字段——
    chain  链路组：分诊 / 命中 / 缺页 / 三入口 / 顺线 / 精筛分 / 注入卡 / 引用
    cost   成本组：各阶段耗时 / LLM 调用数 / token
    health 健康度组：命中 / 缺页 / 注入数 / 精筛顶分 / 水位落后（原始字段；
           聚合指标（命中率/缺页率/回源率等）由 P1 离线计算，公式见 docs/配置与埋点文档.md）

设计约束（E3 定稿）：
- 只写不读：单向输出，不参与线上请求决策；
- JSONL 按天分文件：{TELEMETRY_DIR}/telemetry-YYYY-MM-DD.jsonl（UTF-8，一行一条）；
- 记忆开关全关（B1/B2 档位）时同样记录，候选/注入为空集——基线样本也要进数据集；
- fail-open：写盘失败只告警、绝不打断主链路；写盘为同步追加（单用户单进程，开销可忽略）；
- 每条记录自带配置快照 + 指纹（D10：实验条件绑定），离线回放器（P1）只读本目录。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

from app.core.config import settings
from app.core.logging import logger

SCHEMA_VERSION = 1


def resolve_telemetry_dir() -> str:
    """埋点目录：相对路径基于项目根（settings.BASE_DIR），绝对路径原样使用。"""
    d = settings.TELEMETRY_DIR
    return d if os.path.isabs(d) else os.path.join(settings.BASE_DIR, d)


def current_log_path(date: _dt.date | None = None) -> str:
    """当日 JSONL 文件路径（按本地日期分文件）。"""
    d = date or _dt.date.today()
    return os.path.join(resolve_telemetry_dir(), f"telemetry-{d:%Y%m%d}.jsonl")


def build_trace_record(*, trace_id: str, session_id: str | None = None,
                       chain: dict[str, Any] | None = None,
                       cost: dict[str, Any] | None = None,
                       health: dict[str, Any] | None = None,
                       extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """组装一条完整 trace 记录（不落盘；落盘用 emit_trace）。"""
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "trace_id": trace_id,
        "session_id": session_id,
        "config": {**settings.experiment_snapshot(), "hash": settings.snapshot_hash()},
        "chain": chain or {},
        "cost": cost or {},
        "health": health or {},
    }
    if extra:
        record["extra"] = extra
    return record


def emit_trace(*, trace_id: str, session_id: str | None = None,
               chain: dict[str, Any] | None = None,
               cost: dict[str, Any] | None = None,
               health: dict[str, Any] | None = None,
               extra: dict[str, Any] | None = None) -> bool:
    """记录一次请求的记忆链路，返回是否成功落盘。

    fail-open 约定：任何异常（目录不可建 / 磁盘满 / 权限）都被吞掉并告警，
    绝不影响主链路——埋点可以丢一条，对话不能挂一次。
    """
    if not settings.TELEMETRY_ENABLED:
        return False
    try:
        record = build_trace_record(trace_id=trace_id, session_id=session_id,
                                    chain=chain, cost=cost, health=health, extra=extra)
        _append_line(current_log_path(), json.dumps(record, ensure_ascii=False))
        return True
    except Exception as exc:  # noqa: BLE001 —— fail-open 是本模块的硬约定
        logger.warning(f"埋点写盘失败（已忽略，不影响主链路）: {exc}")
        return False


def _append_line(path: str, line: str) -> None:
    """追加一行 JSONL（独立函数：便于测试注入失败，验证 fail-open）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
