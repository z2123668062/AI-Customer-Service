"""注入格式单测（E10）：标签渲染 / 窗口判重 / 空集 None / 无 ID 泄漏 / 关系与状态标注。"""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.long_term_memory import injector
from app.services.long_term_memory.reranker import ScoredCandidate
from app.services.long_term_memory.retriever import Candidate

UTC = timezone.utc


def _cand(ref_id=1, content="用户对花生严重过敏", kind="entity", source="passive",
          event_time=None, created_at=None, line_kind=None):
    return Candidate(
        ref_id=ref_id, kind=kind, subtype=None, content=content, event_time=event_time,
        source=source, created_at=created_at, routes=frozenset({"entity"}),
        semantic_score=None, line_kind=line_kind,
    )


def _scored(candidate, tier="high", score=0.9):
    return ScoredCandidate(candidate=candidate, tier=tier, score=score, confidence=0.9,
                           reason="stub", origin="model", latency_ms=1.0)


# ================================
# 空集 / 基础渲染
# ================================

def test_empty_returns_none():
    assert injector.render([]) is None


def test_fact_event_raw_rendering():
    """三类形态：事实（月级）· 事件（日级）· 原文（无状态段）。"""
    mems = [
        _cand(1, "用户对花生严重过敏",
              created_at=datetime(2026, 3, 5, tzinfo=UTC)),
        _cand(2, "用户搬到北京朝阳区", kind="event",
              event_time=datetime(2026, 2, 14, tzinfo=UTC)),
        _cand(3, "我打算下周去办签证", kind="raw",
              created_at=datetime(2026, 3, 10, tzinfo=UTC)),
    ]
    out = injector.render(mems)
    assert out.startswith(injector._HEADER)
    assert "- [事实·2026-03·当前] 用户对花生严重过敏" in out
    assert "- [事件·2026-02-14·当前] 用户搬到北京朝阳区" in out
    assert "- [原文·2026-03] 我打算下周去办签证" in out


def test_no_date_and_no_id_leak():
    """无日期卡自动缺省时间段；卡片 ref_id 绝不出现在输出（防编号幻觉）。"""
    out = injector.render([_cand(987654321, "用户有个弟弟叫小明")])
    assert "- [事实·当前] 用户有个弟弟叫小明" in out
    assert "987654321" not in out and "id" not in out.lower()


def test_order_preserved_as_input():
    """渲染不改顺序（排序与截断的职责归 reranker）。"""
    out = injector.render([_cand(2, "第二条"), _cand(1, "第一条")])
    assert out.index("第二条") < out.index("第一条")


def test_scored_candidate_accepted():
    """入参兼容 ScoredCandidate（鸭子取件 .candidate）——生产路径输入形态。"""
    out = injector.render([_scored(_cand(1, "用户喜欢喝咖啡"), tier="mid")])
    assert "用户喜欢喝咖啡" in out


# ================================
# 窗口判重（保守：宁重复勿错杀）
# ================================

def test_window_duplicate_skipped_others_kept():
    """窗口已含该内容 → 跳过；其余卡照常注入。"""
    dup = _cand(1, "我打算下个月去日本看樱花旅游")
    keep = _cand(2, "用户对花生严重过敏")
    out = injector.render([dup, keep], window_texts=["嗯，我打算下个月去日本看樱花旅游呢，可以啊"])
    assert "看樱花" not in out
    assert "花生" in out


def test_window_duplicate_long_overlap_beyond_whole_containment():
    """卡比窗口长：≥10 字连续重合（跨标点）同样算"窗口已有"。"""
    dup = _cand(1, "我打算下个月去日本看樱花旅游，已经订好机票了")
    out = injector.render([dup], window_texts=["我打算下个月，去日本看樱花旅游。"])
    assert out is None


def test_all_filtered_returns_none():
    dup = _cand(1, "用户对花生严重过敏")
    assert injector.render([dup], window_texts=["用户对花生严重过敏，记住了"]) is None


def test_no_window_no_filter():
    """窗口为空（新会话）：不做任何判重。"""
    assert injector.render([_cand(1, "用户对花生严重过敏")], window_texts=[]) is not None


# ================================
# 关系标注 / 状态防御映射
# ================================

def test_relation_labels_by_line_kind():
    out = injector.render([
        _cand(1, "用户开始每周跑步", kind="event", line_kind="caused_by"),
        _cand(2, "用户体检血脂偏高", kind="event", line_kind="led_to"),
        _cand(3, "普通实体卡", line_kind=None),
    ])
    assert "用户开始每周跑步（因果关联）" in out
    assert "用户体检血脂偏高（事件关联）" in out
    assert "普通实体卡" in out and "普通实体卡（" not in out


def test_status_defensive_mapping():
    """候选恒 active（池内/回源已过滤）；防御映射保未来（E11 编辑层）不破格式。"""
    base = dict(content="用户住在上海", kind="entity", event_time=None,
                created_at=None, line_kind=None)
    superseded = SimpleNamespace(ref_id=1, status="superseded", **base)
    revoked = SimpleNamespace(ref_id=2, status="revoked", **base)
    unknown = SimpleNamespace(ref_id=3, status="weird", **base)
    out = injector.render([superseded, revoked, unknown])
    assert "已被取代" in out and "已撤销" in out and out.count("当前") == 1


def test_raw_candidate_has_no_status_segment():
    out = injector.render([_cand(1, "原文内容", kind="raw")])
    assert "[原文] 原文内容" in out and "当前" not in out


# ================================
# trace（E12 埋点输出：chain.injected 的数据源）
# ================================

def test_trace_records_injected_and_skipped():
    """trace：实际注入的 ref_id 与窗口判重跳过的 ref_id 各归其位。"""
    dup = _cand(1, "我打算下个月去日本看樱花旅游")
    keep = _cand(2, "用户对花生严重过敏")
    trace: dict = {}
    out = injector.render([dup, keep],
                          window_texts=["嗯，我打算下个月去日本看樱花旅游呢，可以啊"],
                          trace=trace)
    assert out is not None
    assert trace == {"injected": [2], "skipped_window": [1]}


def test_trace_empty_injected_when_all_filtered():
    """全被窗口判重过滤：injected=[]（chain.injected 空 = 不召回档位口径）。"""
    trace: dict = {}
    out = injector.render([_scored(_cand(1, "用户对花生严重过敏"))],
                          window_texts=["用户对花生严重过敏，记住了"], trace=trace)
    assert out is None
    assert trace == {"injected": [], "skipped_window": [1]}
