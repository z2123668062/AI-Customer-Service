# app/services/long_term_memory/journal.py
"""写路径 · 抽取进度日志（journal；D23）：全局水位的唯一真相载体。

设计（协商定案 D23——替代 D22 的 session_states 表方案）：
- **全局单指针**：水位只有一个数字 = "已抽完到哪条消息"（消息 id 全局递增，跨会话共享）；
- **追加式日志**：每完成一轮抽水（或失败/画线/自检）追加一行人类可读 + 机器可解析的记录；
  水位 = 最后一条含 `watermark=N` 的行的 N——尾读即可恢复，不做覆盖写（无撕裂窗口）；
- **尾行损坏 → 回退上一条**：最坏情形重抽最后一批（方向安全：宁可重抽，不可丢内容）；
- **双落点**：文件（状态真相——logger 有 10 天滚动清理，不能当状态载体）+
  logger（控制台监控与常规日志留痕）；
- **损坏容忍**：文件缺失 = 首启画线（startup_scan 画线跳过存量）；水位超前（换库/删库）=
  启动自检对齐（consolidator.startup_scan）；
- **前提约束**：单进程单写者（多 worker 部署需迁回 DB 载体——记录于 D23）；
- **撕裂窗口**（卡片已提交、journal 未写入 → 该批重抽）按 D23 接受：由"完全相同卡判重
  （E12 接活）+ 取代判定"兜底。

行格式示例（用户定稿叙事）：
    2026-09-29 15:02:11 | 抽水完成 | watermark=123 | 原文=8条 | 卡片=3张（entity:2（preference:1, identity:1）; event:1）| 重复=0（完全同文拦截）| 取代=1 | 连边=2 | 耗时=3.2s | 会话=s1
    2026-09-29 15:02:11 | 抽水失败 | watermark=123 | 原因=TimeoutError: ...（水位保持，下轮自动补抽）
    2026-09-29 15:02:11 | 首启画线 | watermark=42 | 跳过存量 42 条（避免启动即全量重抽；需要补抽用 backfill）

测试：`_journal_file` 可 monkeypatch 注入临时路径；reset_for_tests() 清内存水位。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.core.logging import logger

# —— 模块状态（测试经 monkeypatch / reset_for_tests 清理）——
_journal_file: str | None = None   # 测试注入路径；None = 按 settings 解析
_watermark: int | None = None      # 内存缓存的全局水位（None = 尚未加载）

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_WATERMARK_RE = re.compile(r"watermark=(\d+)")


def resolve_journal_path() -> str:
    """journal 文件路径：测试注入优先；相对路径基于项目根目录（settings.BASE_DIR）。"""
    if _journal_file is not None:
        return _journal_file
    p = settings.EXTRACT_JOURNAL_PATH
    return p if os.path.isabs(p) else os.path.join(settings.BASE_DIR, p)


def _now_str() -> str:
    return datetime.now().strftime(_TS_FORMAT)


# ============ 格式化（写入面）============

def format_record(*, event: str, watermark: int, detail: str = "") -> str:
    """组装一行 journal 记录：`时间 | 事件 | watermark=N | 明细`。"""
    parts = [_now_str(), event, f"watermark={int(watermark)}"]
    if detail:
        parts.append(detail)
    return " | ".join(parts)


def _draft_stats(drafts) -> str:
    """草稿类别分布明细：`entity:2（preference:1, identity:1）; event:1`（空批 → 无）。"""
    kinds: dict[str, int] = {}
    subs: dict[str, dict[str, int]] = {}
    for d in drafts:
        kinds[d.kind] = kinds.get(d.kind, 0) + 1
        if d.subtype:
            subs.setdefault(d.kind, {})
            subs[d.kind][d.subtype] = subs[d.kind].get(d.subtype, 0) + 1
    chunks: list[str] = []
    for kind, n in kinds.items():
        if subs.get(kind):
            inner = ", ".join(f"{s}:{c}" for s, c in subs[kind].items())
            chunks.append(f"{kind}:{n}（{inner}）")
        else:
            chunks.append(f"{kind}:{n}")
    return "; ".join(chunks) if chunks else "无"


def format_success(*, watermark: int, messages: int, drafts, duplicates: int,
                   superseded: int, edges: int, sessions: set[str] | None = None,
                   elapsed: float = 0.0) -> str:
    """抽水成功行（D23 定稿叙事：截止 id / 用了多少原文 / 形成多少卡 / 类别分布 / 判重拦截）。"""
    detail = (f"原文={int(messages)}条 | 卡片={len(drafts)}张（{_draft_stats(drafts)}）| "
              f"重复={int(duplicates)}（完全同文拦截）| 取代={int(superseded)} | "
              f"连边={int(edges)} | 耗时={elapsed:.1f}s")
    if sessions:
        detail += f" | 会话={'/'.join(sorted(sessions))}"
    return format_record(event="抽水完成", watermark=watermark, detail=detail)


def format_failure(*, watermark: int, error: str) -> str:
    """抽水失败行：水位保持不变（下轮自动补抽；方向安全——失败不推进）。"""
    brief = " ".join((error or "unknown").split())[:160]
    return format_record(event="抽水失败", watermark=watermark,
                         detail=f"原因={brief}（水位保持，下轮自动补抽）")


# ============ 解析与尾读（读取面）============

def parse_record(line: str) -> dict[str, Any] | None:
    """解析一行 journal 记录（宽松：只要含 `watermark=N` 即有效）。

    返回 {watermark, ts?, event?, raw}；不可解析（空行 / 损坏行 / 无水位）→ None。
    """
    m = _WATERMARK_RE.search(line)
    if not m:
        return None
    rec: dict[str, Any] = {"watermark": int(m.group(1)), "raw": line.rstrip("\n")}
    if _TS_RE.match(line):
        rec["ts"] = line[:19]
    parts = [p.strip() for p in line.split("|")]
    if len(parts) >= 2:
        rec["event"] = parts[1]
    return rec


def read_last_record() -> dict[str, Any] | None:
    """尾读最后一条有效记录（从尾往回跳过坏行）；文件缺失 / 无有效行 → None。

    规模说明：单用户写路径每天几十行，全读足够（不做 seek 尾读的过早优化）。
    读失败按"无记录"处理——上层据此画线（跳过存量），方向安全。
    """
    path = resolve_journal_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning(f"[journal] 读取失败（按无记录处理）: {e}")
        return None
    for line in reversed(lines):
        rec = parse_record(line)
        if rec is not None:
            return rec
    return None


def read_watermark() -> int | None:
    """当前文件水位（最后一条有效记录的 watermark）；无 → None。"""
    rec = read_last_record()
    return rec["watermark"] if rec is not None else None


# ============ 追加与内存水位（写入面）============

def append_record(line: str) -> bool:
    """追加一行（文件 append + logger 双落点）；返回是否成功。

    - 文件 = 状态真相（logger 有滚动清理，不能当状态载体）；logger = 控制台/日志留痕；
    - 失败方向：追加失败**不得推进水位** → 下轮重抽（宁可重抽，不可丢）。
    """
    path = resolve_journal_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning(f"[journal] 追加失败（水位将不推进，下轮重抽）: {e}")
        return False
    logger.info(f"[journal] {line}")
    return True


def advance(watermark: int, line: str) -> bool:
    """推进水位：先写文件，成功才更新内存缓存；失败不推进（返回 False）。"""
    global _watermark
    if not append_record(line):
        return False
    _watermark = int(watermark)
    return True


def current() -> int:
    """当前全局水位（内存缓存；未加载时自动尾读；文件无记录 → 0 = 尚未抽过）。"""
    global _watermark
    if _watermark is None:
        w = read_watermark()
        _watermark = 0 if w is None else w
    return _watermark


def prime(watermark: int) -> None:
    """直接设定内存水位（首启画线 / 自检对齐后调用；不写文件——写由调用方决定）。"""
    global _watermark
    _watermark = int(watermark)


def reset_for_tests() -> None:
    """清内存水位（测试 fixture 调用；生产不使用）。"""
    global _watermark
    _watermark = None
