# app/services/long_term_memory/injector.py
"""读路径 · 注入格式：把过门槛记忆渲染为可拼进生成 prompt 的文本段（E10 交付）。

格式定稿（D25）：
    【记忆参考】以下是关于用户的已知信息，回答时自然结合，勿生硬复述：
    - [事实·2026-03·当前] 用户对花生严重过敏
    - [事件·2026-02-14·当前] 用户搬到北京朝阳区（因果关联）
    - [原文·2026-03] "我打算下周去办签证"

三条纪律（工程路线 E10 改动点）：
- **不暴露卡片 ID**——不给模型看 ref_id（防"编号幻觉"，卡片对模型是隐形物）；
- **窗口已有的不重复注入**——原文级重复检测（保守：宁可重复注入，不可错杀记忆）；
- **空集 / 全被过滤 → None**——"不注入" = B2 生成行为，由调用方统一处理。

纯确定性渲染（零 LLM、零 IO）；与注入格式相关的取舍全部收在这一个文件里
（拼接位置在 generation_service，架构硬约束 6——两处各自的唯一职责）。
"""
from __future__ import annotations

import re
from typing import Any, Sequence

# ============================================================
# 格式常量（改格式 = 改实验条件，随快照登记；见精筛注入文档 §2）
# ============================================================

_HEADER = "【记忆参考】以下是关于用户的已知信息，回答时自然结合，勿生硬复述："

_TYPE_LABELS = {"entity": "事实", "event": "事件", "raw": "原文"}
_STATUS_LABELS = {"active": "当前", "superseded": "已被取代", "revoked": "已撤销"}
_RELATION_LABELS = {"caused_by": "（因果关联）", "led_to": "（事件关联）"}

_WINDOW_OVERLAP_MIN = 10   # 窗口判重：归一化后 ≥10 字连续重合视为"窗口已有"（保守初值）


# ============================================================
# 窗口判重（确定性：归一化 + 长子串重合——保守方向）
# ============================================================

def _norm(text: str) -> str:
    """归一化：去空白与标点（保留中英文数字）+ casefold——重合检测跨标点差异。"""
    return re.sub(r"[\W_]+", "", text).casefold()


def _is_window_duplicate(text: str, window_norm: str) -> bool:
    """窗口已有检测：归一化后卡全文被窗口包含，或存在 ≥_WINDOW_OVERLAP_MIN 字连续重合。

    判据刻意保守（长重合才算）：重复注入的代价只是一点 token；错杀则是把有效
    记忆从模型视野里抹掉——方向按"宁重复、勿错杀"（对应读侧 fail-safe 哲学）。
    主要命中场景：L2 原文候选（内容就是原话本身）与近原句的卡在窗口未滚出时。
    """
    if not window_norm:
        return False
    cn = _norm(text)
    if not cn:
        return False
    if cn in window_norm:
        return True
    n = _WINDOW_OVERLAP_MIN
    return any(cn[i:i + n] in window_norm for i in range(len(cn) - n + 1))


# ============================================================
# 单条渲染
# ============================================================

def _time_label(candidate: Any) -> str | None:
    """时间标签：**只认事件时间**（日级）——没有事件时间就不打时间标签。

    L2 定稿（2026-10-30）：原实现回落到 created_at（入库月份），在 LoCoMo 上把 2023 年的事
    标成 ·2026-10·，模型据此答出 "This month (October 2026)"。**入库时间不是事件时间**，
    二者混淆比"没有时间"更危险——没有标签模型会说"未提及"，错标签会造出确定性的错误答案。
    精确时间由检索层（时间窗口）与原文档案承担。
    """
    if candidate.event_time is not None:
        return candidate.event_time.strftime("%Y-%m-%d")
    return None


def _render_line(candidate: Any, text: str) -> str:
    """单条格式：`- [类型·时间·状态] 内容（关系标注）`（可省略段自动缺省）。"""
    parts = [_TYPE_LABELS.get(candidate.kind, "记忆")]
    ts = _time_label(candidate)
    if ts:
        parts.append(ts)
    if candidate.kind != "raw":   # 原文候选没有卡片状态
        status = getattr(candidate, "status", "active")   # 候选恒 active；防御性映射
        parts.append(_STATUS_LABELS.get(status, "当前"))
    relation = _RELATION_LABELS.get(candidate.line_kind or "", "")
    return f"- [{'·'.join(parts)}] {text}{relation}"


# ============================================================
# 主入口
# ============================================================

def render(memories: Sequence[Any], *, window_texts: Sequence[str] = (),
           trace: dict[str, Any] | None = None) -> str | None:
    """渲染注入段（输入 = reranker 的 kept，已排序截断；可能为空 → None）。

    - memories：`ScoredCandidate` 列表（也接受裸 `Candidate`——鸭子取件 `.candidate`）；
    - window_texts：当前短记忆窗口的文本（判重用；不含本轮用户消息——调用方在
      用户消息入窗之前取值，"窗口已有"指历史窗口）；
    - trace：可选埋点输出（E12 组装链）——提供时写入本环节过程信息：`injected`
      （**实际进入注入文本**的 ref_id 列表，空 = 不召回档位）/ `skipped_window`
      （窗口判重跳过的 ref_id）。纯输出参数：不传零开销、行为不变。
    顺序保持不变（排序与截断的职责归 reranker，本函数只渲染与过滤）。
    """
    window_norm = _norm(" ".join(window_texts))
    lines: list[str] = []
    injected_ids: list[Any] = []
    skipped_window_ids: list[Any] = []
    for item in memories:
        candidate = getattr(item, "candidate", item)   # ScoredCandidate / Candidate 通吃
        text = candidate.content.strip()
        if not text:
            continue
        if _is_window_duplicate(text, window_norm):
            skipped_window_ids.append(candidate.ref_id)
            continue
        lines.append(_render_line(candidate, text))
        injected_ids.append(candidate.ref_id)
    if trace is not None:
        trace["injected"] = injected_ids
        trace["skipped_window"] = skipped_window_ids
    if not lines:
        return None   # 空集 / 全被过滤 = "不注入"（B2 生成行为的统一入口）
    return _HEADER + "\n" + "\n".join(lines)
