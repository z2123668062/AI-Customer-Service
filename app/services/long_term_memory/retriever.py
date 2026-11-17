# app/services/long_term_memory/retriever.py
"""读路径：一次检索 = 一次缺页处理（系统设计.md §4）。

链路（L12 起无分诊总闸——每轮必检索）：
    抽线索（实体反向匹配 + 引号启发式 + 时间词表；零 LLM、零 IO）
      → 扫快表（by_entities + by_time_range；零 IO）
        ├─ 有收获（≥1）→ hit：本地候选，不回源；补一趟语义分做粗排
        └─ 全空 → fault（缺页）：
              L1 = DB 实体路 + DB 时间路（共用一次扫描，SOURCE_SCAN_LIMIT 封顶）
                   ∥ 语义路（embed + Qdrant top-k，相似度下限采信）
              L1 全空 → L2 = 原文关键词兜底（search_messages；候选 kind="raw"）
        回填（fault）：refill 卡 + 恢复边
      → 粗排预算（按语义分截断至 RETRIEVAL_COARSE_TOPK）
      → 顺线一跳（只顺因果线；池内免费全收 + 池外小预算预取）
      → 原文通道（命中路径补带相关原文）→ 交精筛

设计要点：
- **分诊已删除（L12）**：真实对话上它把"看起来像通用问题、其实指向用户自己"的查询判成
  skip，整条检索被关掉、模型随后用常识编答案；省下的 1.2 秒不值这个代价。
- **"够格"口径 = 本地任一入口有收获（≥1）即 hit**：不回源；好坏由精筛兜底。
- **快表只做确定性索引**（实体倒排 + 时间轴，微秒级）。
- **语义路两种时机（L1 定稿 2026-10-30）**：① 缺页回源时做，决定"L1 是否算空 / 是否下探 L2"；
  ② **命中路径也做一次**，用途是粗排打分——原实现"命中即不回源"会把语义路整条短路，
  候选失去相关性信号（_rank_key 退化成按 id 新优先），只能让精筛逐张兜底。
- **粗排候选预算 = 工作集上限**（config.RETRIEVAL_COARSE_TOPK）：实体倒排命中量与"该实体的
  覆盖卡数"同阶（单用户场景下用户本名往往覆盖大半卡片），不设预算就等于把 O(N) 张卡全送精筛
  （每张一次 LLM 调用）。截断只是预算，不是筛选：被截断的卡仍在册，别的轮次照样能进来。
- 顺线只顺因果（caused_by / led_to），**跳过 supersedes（版本边不是证据扩展）**。

降级纪律（读路径不因外设故障而崩）：
- gate 内部保证不抛（fail-safe=yes → 照走回源）；
- embedding / Qdrant 异常 → 语义路降级为空，其余路照走（不改变 L2 下探判据）；
- DB 异常向上抛（底座问题必须暴露）；本模块只读，唯一"写"是内存 refill。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.services.long_term_memory import gate, pool, store

# ============================================================
# 检索参数（模块常量；config 已有的不重复：SOURCE_SCAN_LIMIT / LINE_PREFETCH_LIMIT）
# ============================================================

_SEMANTIC_TOPK = 20            # 语义路 top-k（L1 与 flat 档同用）
_SEMANTIC_COARSE_TOPK = 300    # 粗排打分覆盖面：给命中路径的全部候选打语义分（覆盖快表容量量级）
_L1_SEMANTIC_MIN_SCORE = 0.30  # 语义分采信下限：低于不采信（决定 L1 是否算"空"→ 是否下探 L2）
_LINE_ADD_LIMIT = 10           # 顺线池外预取总量封顶（防"候选 × 邻居"冲爆快表容量）
_L2_KEYWORD_LIMIT = 8          # L2 关键词上限（实体名 + 英文词 + 2-gram 去重后截断）
_RAW_MIN_HITS = 2              # 原文候选门槛：至少命中两个关键词才收（单词命中噪声太大）
_SEMANTIC_COOLDOWN_S = 60.0    # 语义路熔断冷却：一次失败后该窗口内跳过（不重复白付 embed 成本）

_CAUSAL_KINDS = ("caused_by", "led_to")   # 顺线只顺因果（跳过 supersedes 版本边）
_ROUTE_RANK = {"entity": 0, "semantic": 1, "time": 2, "line": 3, "raw": 4}  # 粗排入口优先级

_semantic_down_until = 0.0     # 语义路熔断截止（time.monotonic 口径；0 = 可用）


# ============================================================
# 数据契约（E10 精筛的输入单元 / trace 直接喂埋点 chain 组）
# ============================================================

@dataclass(frozen=True)
class Candidate:
    """候选记忆（E9 只收集证据、不判好坏——取舍归 E10 精筛）。

    - ref_id：统一 ID 空间——卡片候选 = cards.id；原文候选（kind="raw"）= messages.id
    - routes：证据链（entity / time / semantic / line / raw 的并集）——供精筛与埋点归因
    - semantic_score：语义相似度（仅 semantic 路有值，其余 None）
    - line_kind：顺线来源（caused_by / led_to；仅 line 路有值，其余 None）
    """
    ref_id: int
    kind: str                      # entity / event / raw（L2 原文候选）
    subtype: str | None
    content: str
    event_time: datetime | None
    source: str                    # passive / explicit；原文候选固定 "raw"
    created_at: datetime | None
    routes: frozenset[str]
    semantic_score: float | None
    line_kind: str | None


@dataclass(frozen=True)
class RetrievalResult:
    """一次检索的完整产出：候选列表（可能为空）+ trace（E12 组装时并入 emit_trace 的 chain 组）。"""
    candidates: list[Candidate]
    trace: dict[str, Any]


# ============================================================
# 抽线索 · 时间词（纯规则表；零 LLM。窗口按 now 的日历切分，周一起始）
# ============================================================
# 口径说明：now 由调用方给 aware datetime（默认 UTC）；DB 读回 naive 视为 UTC（_coerce_utc）。
# 日历按 UTC 切分是实验期简化（本地时区相位差 < 1 天，不影响窗口量级判断）——见读路径文档。

_TIME_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"今天|今日"), "today"),
    (re.compile(r"昨天|昨日"), "yesterday"),
    (re.compile(r"前天"), "before_yesterday"),
    (re.compile(r"明天|明日"), "tomorrow"),
    (re.compile(r"上周|上星期"), "last_week"),
    (re.compile(r"本周|这周|本星期|这星期"), "this_week"),
    (re.compile(r"上个月|上月"), "last_month"),
    (re.compile(r"本月|这个月|这月"), "this_month"),
    (re.compile(r"去年|上一年"), "last_year"),
    (re.compile(r"今年|本年"), "this_year"),
    (re.compile(r"\byesterday\b", re.I), "yesterday"),
    (re.compile(r"\btoday\b", re.I), "today"),
    (re.compile(r"\btomorrow\b", re.I), "tomorrow"),
    (re.compile(r"\blast week\b", re.I), "last_week"),
    (re.compile(r"\blast month\b", re.I), "last_month"),
    (re.compile(r"\blast year\b", re.I), "last_year"),
)


def _next_month(first_day: datetime) -> datetime:
    """某月 1 日 → 下月 1 日。"""
    if first_day.month == 12:
        return first_day.replace(year=first_day.year + 1, month=1)
    return first_day.replace(month=first_day.month + 1)


def _prev_month(first_day: datetime) -> datetime:
    """某月 1 日 → 上月 1 日。"""
    if first_day.month == 1:
        return first_day.replace(year=first_day.year - 1, month=12)
    return first_day.replace(month=first_day.month - 1)


def _resolve_window(kind: str, now: datetime) -> tuple[datetime, datetime]:
    """时间词 → [start, end) 窗口（半开区间；周一为一周之始；月/年按日历边界）。"""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "today":
        return day, day + timedelta(days=1)
    if kind == "yesterday":
        return day - timedelta(days=1), day
    if kind == "before_yesterday":
        return day - timedelta(days=2), day - timedelta(days=1)
    if kind == "tomorrow":
        return day + timedelta(days=1), day + timedelta(days=2)
    week_start = day - timedelta(days=day.weekday())
    if kind == "last_week":
        return week_start - timedelta(days=7), week_start
    if kind == "this_week":
        return week_start, week_start + timedelta(days=7)
    month_start = day.replace(day=1)
    if kind == "last_month":
        return _prev_month(month_start), month_start
    if kind == "this_month":
        return month_start, _next_month(month_start)
    year_start = day.replace(month=1, day=1)
    if kind == "last_year":
        return year_start.replace(year=year_start.year - 1), year_start
    # this_year
    return year_start, year_start.replace(year=year_start.year + 1)


def parse_time_window(query: str, now: datetime) -> tuple[datetime, datetime] | None:
    """原话 → 时间窗口（首个命中的时间词为准；无时间词 → None）。规则表见模块头注释。"""
    for pattern, kind in _TIME_RULES:
        if pattern.search(query):
            return _resolve_window(kind, now)
    return None


# ============================================================
# 抽线索 · 实体（反向匹配：先有词表、再在话里找词——中文无分词下的确定性做法）
# ============================================================

_QUOTED_RE = re.compile(r"[「『“\"']([^「」『』“”\"']{1,20})[」』”\"']")


def match_known_entities(query: str, known: Sequence[str]) -> list[str]:
    """反向匹配：已知实体键（快表归一化小写）逐一出现在原话中 → 线索。

    与 by_entities 的"话里有词、去表里查"互为反向；漏匹配由语义路兜底（漏不致命）。
    """
    text = query.casefold()
    return [key for key in known if key and key in text]


def _quoted_names(query: str) -> list[str]:
    """引号启发式：被引号/书名号括起的短串按专名处理（可能不在已知词表——照样当线索）。"""
    return [name.strip() for name in _QUOTED_RE.findall(query) if name.strip()]


def _extract_clues(query: str, now: datetime) -> tuple[list[str], tuple[datetime, datetime] | None]:
    """抽线索（纯确定性，零 LLM、零 IO）：实体反向匹配 + 引号启发式 + 时间词表。

    依赖仅 pool.known_entities()（纯内存只读）——线索量级小，不需要模型。
    """
    entities = match_known_entities(query, pool.known_entities())
    seen = {entity.casefold() for entity in entities}
    for name in _quoted_names(query):
        key = name.casefold()
        if key not in seen:
            entities.append(name)
            seen.add(key)
    return entities, parse_time_window(query, now)

# ============================================================
# 抽线索 · L2 关键词（兜底扫描的弹药：实体名 + 英文词 + 汉字 2-gram）
# ============================================================

_STOP_CHARS = frozenset("的了吗呢吧啊嗯呀我你他她它们是在")

# 英文停用词（L6）：原实现只拦汉字停用字，英文按「长度≥3」全收，
# 于是 what / did / the 这些词也进了关键词表，SQL 的 OR LIKE 一查命中一片，L2 等于没筛。
_STOP_WORDS_EN = frozenset(
    "the and for are but not you all any can had her was one our out has him his how its may "
    "new now old see two way who did get has let put say she too use was wer what when where "
    "which while with would your from they them then than that this these those have been "
    "being does doing done about after before into over under between during there here "
    "could should will shall upon such same other more most some very much many".split()
)


def build_l2_keywords(query: str, entities: Sequence[str]) -> list[str]:
    """L2 关键词：实体名优先 + 英文词（≥3 字母）+ 汉字 2-gram（滤停用字），去重截断。

    2-gram 是"没有分词器的分词"——宁多勿漏（多一个词只是多一个 OR LIKE，成本可控）；
    语义关联的遗漏由 L1 的语义路承担，这里只负责"把原话里的词捞出来"。
    """
    keywords: list[str] = []
    seen: set[str] = set()

    def _push(word: str) -> None:
        key = word.strip()
        if key and key.casefold() not in seen:
            seen.add(key.casefold())
            keywords.append(key)

    for name in entities:
        _push(name)
    for token in re.findall(r"[A-Za-z]{3,}", query):
        if token.casefold() not in _STOP_WORDS_EN:
            _push(token)
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram[0] in _STOP_CHARS or gram[1] in _STOP_CHARS:
                continue
            _push(gram)
    return keywords[:_L2_KEYWORD_LIMIT]


# ============================================================
# 工具：时区归一 / 候选构造 / 粗排 / trace 骨架 / 候选累积
# ============================================================

def _coerce_utc(dt: datetime) -> datetime:
    """SQLite 读回的 datetime 可能无 tzinfo（对齐 consolidator 口径）：naive 视为 UTC。"""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _candidate_from_card(card: Any, routes: set[str] | frozenset[str],
                         semantic_score: float | None = None,
                         line_kind: str | None = None) -> Candidate:
    """卡（ORM Card 或 PoolCard，鸭子类型同形）→ 候选。"""
    return Candidate(
        ref_id=card.id, kind=card.kind, subtype=card.subtype, content=card.content,
        event_time=card.event_time, source=card.source, created_at=card.created_at,
        routes=frozenset(routes), semantic_score=semantic_score, line_kind=line_kind,
    )


def _candidate_from_message(msg: Any) -> Candidate:
    """L2 原文候选（kind="raw"；ref_id = messages.id——与卡片不同 ID 空间，见 Candidate 注释）。"""
    return Candidate(
        ref_id=msg.id, kind="raw", subtype=None, content=msg.content,
        event_time=None, source="raw", created_at=msg.created_at,
        routes=frozenset({"raw"}), semantic_score=None, line_kind=None,
    )


def _rank_key(candidate: Candidate) -> tuple[int, float, int]:
    """粗排（E9 的职责边界：排秩序、不判好坏——好坏归 E10 精筛）。

    ① 入口优先级（entity > semantic > time > line > raw——专名精确匹配最可信）；
    ② 语义分降序；③ id 新优先。确定性、无魔法权重（同 D18 哲学）。
    """
    rank = min((_ROUTE_RANK.get(route, 9) for route in candidate.routes), default=9)
    return (rank, -(candidate.semantic_score or 0.0), -candidate.ref_id)


def _empty_trace() -> dict[str, Any]:
    """trace 骨架（全字段恒定出现——下游不做存在性判断；E12 并入埋点 chain 组）。

    字段对齐《配置与埋点文档》§2.3（chain）与 §2.4（cost.stage_ms）；
    原 has_hook 字段随"分诊在前"修订并入 gate 详情（D24——钩子话术单归 gate 前置正则）。
    """
    return {
        "triage": None,            # hit（快表命中）/ fault（缺页回源）——仅观测口径
        "gate": None,              # 分诊已删（L12），字段恒 None——保留以兼容旧 telemetry
        "entities": [],            # 抽到的实体线索（含引号启发式）
        "entities_missing": [],    # 快表未收录的实体（一级缺页线索）
        "time_window": None,       # [start_iso, end_iso]
        "hit_cards": [],           # 快表命中卡 id（埋点 chain 字段）
        "pool_hits": {"entity": 0, "time": 0},
        "fault_level": None,       # L1（L1 有收获）/ L2（下探了原文兜底）
        "l1": {"entity": 0, "time": 0, "semantic": 0, "refilled": 0},
        "l2": {"messages": 0},
        "line": {"collected": 0, "fetched": 0},
        "candidates": {"entity": 0, "semantic": 0, "time": 0, "line": 0, "raw": 0},
        "merged_count": 0,
        # 粗排候选预算（L1/L2 定稿）：semantic=scored/unavailable；total/kept/dropped 便于观测预算是否吃紧
        "coarse": {"semantic": None, "total": 0, "kept": 0, "dropped": 0,
                   "min_kept_score": None, "semantic_added": 0},
        "stage_ms": {"triage": 0.0, "retrieval": 0.0},
    }


def _accumulate(accum: dict[int, dict], card: Any, routes: set[str] | frozenset[str],
                *, score: float | None = None, line_kind: str | None = None) -> None:
    """候选累积器：同 id 合并 routes 并集（证据链）；语义分 / 顺线来源取新值。"""
    item = accum.get(card.id)
    if item is None:
        accum[card.id] = {"card": card, "routes": set(routes),
                          "score": score, "line_kind": line_kind}
        return
    item["routes"].update(routes)
    if score is not None:
        item["score"] = score
    if line_kind is not None:
        item["line_kind"] = line_kind


def _scan_pool(entities: Sequence[str], window: tuple[datetime, datetime] | None,
               ) -> tuple[list[int], list[str], list[int]]:
    """扫快表（零 IO）：实体入口 + 时间入口。

    返回 (实体命中 id, 未收录实体, 时间命中 id)；"未收录实体"即一级缺页线索（池只报告）。
    """
    entity_hits: list[int] = []
    missing: list[str] = []
    if entities:
        entity_hits, missing = pool.by_entities(entities)
    time_hits = pool.by_time_range(*window) if window is not None else []
    return entity_hits, missing, time_hits


# ============================================================
# 语义路（L1 与 flat 档共用）；embed + Qdrant，无会话依赖
# ============================================================

async def _semantic_raw(query: str, *, limit: int) -> list[tuple[int, float]]:
    """语义路原样输出：embed → Qdrant top-k（**不做采信下限过滤**）。

    两种用途共用这一趟：
    - 采信口径（L1 的"语义路是否算空"）→ 由 _semantic_search 施加下限；
    - 粗排打分（命中路径给候选排次序）→ 需要全部相对分数，不能先砍掉低分者。
    任何异常（embedding / Qdrant）→ 降级为空并告警；失败后进入冷窗，跳过重复 embed。
    """
    global _semantic_down_until
    if not settings.QDRANT_ENABLED:
        return []
    if time.monotonic() < _semantic_down_until:
        return []
    try:
        vector = (await store.embed_texts([query]))[0]
        hits = store.get_qdrant().search(vector, limit=limit)
    except Exception as exc:  # noqa: BLE001 —— 降级是设计的一部分（不抛）
        _semantic_down_until = time.monotonic() + _SEMANTIC_COOLDOWN_S
        logger.warning(f"[retriever] 语义路降级（embedding/Qdrant 不可用）: {exc}")
        return []
    _semantic_down_until = 0.0
    return [(card_id, float(score)) for card_id, score in hits]


async def _semantic_search(query: str) -> list[tuple[int, float]]:
    """语义路（采信口径）：top-k + 相似度下限过滤（返回 [(card_id, score)]）。

    "低于下限不采信"决定 L1 是否算"空"（即是否下探 L2）——防 top-k 永远非空使 L2 永不触发。
    """
    return [(card_id, score) for card_id, score in
            await _semantic_raw(query, limit=_SEMANTIC_TOPK)
            if score >= _L1_SEMANTIC_MIN_SCORE]


# ============================================================
# 缺页回源：L1（三入口）/ L2（原文兜底）
# ============================================================

async def _source_l1(session: AsyncSession, query: str, entities: Sequence[str],
                     window: tuple[datetime, datetime] | None,
                     ) -> tuple[list[Any], list[Any], list[tuple[Any, float]]]:
    """L1 回源：DB 实体路 + DB 时间路（共用一次扫描）+ 语义路（与扫描并发）。

    成本纪律：两条 DB 路共用一次"最近 SOURCE_SCAN_LIMIT 张"的扫描（回源硬顶，防扫穿全库）；
    并发口径：语义路前半程不碰会话，与扫描 asyncio 并发（AsyncSession 不可并发——gather 中
    只有扫描用会话）；语义路取正文再走会话（串行）。
    """
    scan_cards, semantic_ids = await asyncio.gather(
        store.list_cards(session, limit=settings.SOURCE_SCAN_LIMIT, desc=True),
        _semantic_search(query),
    )
    ent_keys = {e.strip().casefold() for e in entities if e.strip()}
    l1_entity: list[Any] = []
    l1_time: list[Any] = []
    for card in scan_cards:
        card_keys = {str(x).strip().casefold() for x in (card.entities or [])}
        if ent_keys and ent_keys & card_keys:
            l1_entity.append(card)
        if window is not None and card.event_time is not None:
            ts = _coerce_utc(card.event_time)
            if window[0] <= ts < window[1]:
                l1_time.append(card)
    l1_semantic: list[tuple[Any, float]] = []
    for card_id, score in semantic_ids:
        card = await store.get_card(session, card_id)
        if card is not None and card.status == store.ACTIVE:
            l1_semantic.append((card, score))
    return l1_entity, l1_time, l1_semantic


_NORM_RE = re.compile(r"[\W_]+")
_SPEAKER_PREFIX_RE = re.compile(r"^\s*[A-Za-z][\w' .]{0,24}:\s*")


def _norm_text(text: str) -> str:
    """归一化（去空白标点、统一大小写）——原文与卡片的重复检测用。"""
    return _NORM_RE.sub("", text or "").casefold()


async def _source_l2(session: AsyncSession, query: str, entities: Sequence[str]) -> list[Any]:
    """L2 原文兜底（原始版，保留给测试与回源调用）：关键词 OR LIKE 扫 messages。"""
    keywords = build_l2_keywords(query, entities)
    if not keywords:
        return []
    return await store.search_messages(session, keywords=keywords, limit=settings.SOURCE_SCAN_LIMIT)


async def _merge_raw(session: AsyncSession, query: str, entities: Sequence[str],
                     accum: dict[int, dict], trace: dict[str, Any], *, topk: int,
                     min_hits: int | None = None) -> list[Candidate]:
    """原文通道（L8）：把相关原文一并带上，补回卡片抽丢的细节。

    为什么放在命中路径上：卡片是有损抽象（"四个活动"被压成"喜欢各种活动"），而原文是无损的。
    原实现把这条通道锁在"快表全空"分支里，44 道题只撞上 1 次，等于没实现。

    三条纪律：
    - 用命中关键词个数排序，不是按新旧——原文检索没有向量分，关键词数是最便宜的替代信号；
    - 至少命中两个关键词才收，单词命中（"caroline"）会把半个库捞出来；
    - 已经被某张候选卡片覆盖的原文不收——同一条信息既给卡又给原文是纯浪费 token。

    门槛分两种：命中路径有卡片兜底，原文只是补充，要求命中两个词才收；
    纯缺页路径上原文是唯一证据，只命中一个词也要收（宁多勿漏）。
    """
    threshold = _RAW_MIN_HITS if min_hits is None else min_hits
    keywords = build_l2_keywords(query, entities)
    if not keywords:
        return []
    messages = await store.search_messages(session, keywords=keywords,
                                          limit=settings.SOURCE_SCAN_LIMIT)
    if not messages:
        return []
    card_texts = [t for item in accum.values()
                  if (t := _norm_text(getattr(item["card"], "content", "")))]
    folded = [k.casefold() for k in keywords]
    # 英文关键词要求词首边界：否则 self 会命中 yourself、care 会命中 scare，
    # 捞回来的全是闲聊（实测 q84 就是这样把三条无关对话带回来的）。
    patterns = [(re.compile(rf"\b{re.escape(k)}") if k.isascii() else None, k) for k in folded]
    scored: list[tuple[int, int, Any]] = []
    for msg in messages:
        text = msg.content or ""
        norm = _norm_text(text)
        if not norm:
            continue
        if any(norm in ct or ct in norm for ct in card_texts):
            continue                                   # 卡片已覆盖这条原文
        # 匹配前剥掉说话人前缀（"Melanie: "）：每行都带，等于给每条消息白送一个命中，
        # 把 6 个候选名额浪费在寒暄上。
        low = _SPEAKER_PREFIX_RE.sub("", text).casefold()
        hits = sum(1 for pat, k in patterns if (pat.search(low) if pat else k in low))
        if hits < threshold:
            continue
        scored.append((hits, msg.id, msg))
    scored.sort(key=lambda x: (-x[0], -x[1]))          # 命中多的优先，同分取较新的
    picked = scored[:topk]
    trace["l2"]["messages"] = len(picked)
    return [_candidate_from_message(m) for _, _, m in picked]


# ============================================================
# 回填（fault 档收尾：卡入池 + 恢复边）与顺线一跳
# ============================================================

async def _backfill(session: AsyncSession, cards: list[Any]) -> None:
    """回填：卡先入池，再恢复"这批卡的全部线"（两端在册才装）。

    兑现快表文档 §4 留给 E9 的边恢复定稿项——恢复后本次顺线即可看见这些卡与
    在册卡/相互之间的因果线；下次同查询直接由快表命中（复用证据）。
    """
    pool.refill(cards)
    edges: list[tuple[int, int, str]] = []
    for card in cards:
        for edge in await store.list_edges(session, card_id=card.id):
            edges.append((edge.from_card_id, edge.to_card_id, edge.kind))
    if edges:
        pool.refill(edges=edges)


async def _line_expand(session: AsyncSession, direct_cards: list[Any], direct_ids: set[int],
                       accum: dict[int, dict], trace: dict[str, Any]) -> None:
    """顺线一跳：只顺因果线（caused_by / led_to；跳过 supersedes 版本边）。

    - 池内邻居免费全收（neighbors 镜像；每卡 ≤ LINE_PREFETCH_LIMIT）；
    - 未满额时查 DB 补齐：池内漏网（tier_of 兜底）免费收；池外小预算预取
      （_LINE_ADD_LIMIT 封顶——防"候选 × 邻居"冲爆快表容量），预取回填时连边一起装回。
    """
    collected: dict[int, str] = {}                 # 池内顺线新增：邻居 id → kind
    fetch_pairs: list[tuple[int, int, str]] = []   # 池外预取：(来源卡, 邻居, kind)
    fetch_ids: set[int] = set()
    per_card = settings.LINE_PREFETCH_LIMIT
    for card in direct_cards:
        picked = 0
        for neighbor_id, kind in pool.neighbors(card.id):
            if kind not in _CAUSAL_KINDS or neighbor_id in direct_ids or neighbor_id in collected:
                continue
            collected[neighbor_id] = kind
            picked += 1
            if picked >= per_card:
                break
        if picked >= per_card:
            continue
        for edge in await store.list_edges(session, card_id=card.id):
            if edge.kind not in _CAUSAL_KINDS:
                continue
            neighbor_id = edge.to_card_id if edge.from_card_id == card.id else edge.from_card_id
            if neighbor_id in direct_ids or neighbor_id in collected or neighbor_id in fetch_ids:
                continue
            if pool.tier_of(neighbor_id) is not None:   # 在池但边未在镜像（兜底）→ 免费收
                collected[neighbor_id] = edge.kind
            elif len(fetch_pairs) < _LINE_ADD_LIMIT:
                fetch_pairs.append((card.id, neighbor_id, edge.kind))
                fetch_ids.add(neighbor_id)
            else:
                continue                                # 预算尽：池外不再预取（池内照收）
            picked += 1
            if picked >= per_card:
                break
    if collected:
        hits, _ = pool.get_cards(sorted(collected))
        for pool_card in hits:
            _accumulate(accum, pool_card, {"line"}, line_kind=collected[pool_card.id])
    fetched: list[Any] = []
    fetched_edges: list[tuple[int, int, str]] = []
    for from_id, neighbor_id, kind in fetch_pairs:
        neighbor = await store.get_card(session, neighbor_id)
        if neighbor is None or neighbor.status != store.ACTIVE:
            continue
        fetched.append(neighbor)
        fetched_edges.append((from_id, neighbor_id, kind))
    if fetched:
        pool.refill(fetched, fetched_edges)             # 预取回填（卡 + 边同批；两端在册才装）
    kind_of = {neighbor_id: kind for _, neighbor_id, kind in fetch_pairs}
    for neighbor in fetched:
        _accumulate(accum, neighbor, {"line"}, line_kind=kind_of[neighbor.id])
    trace["line"] = {"collected": len(collected), "fetched": len(fetched)}


def _finalize(accum: dict[int, dict], l2_raw: list[Candidate]) -> list[Candidate]:
    """候选收口：卡候选（按 id 稳定序）+ 原文候选 → 粗排。"""
    cards = [_candidate_from_card(accum[k]["card"], accum[k]["routes"],
                                  accum[k]["score"], accum[k]["line_kind"])
             for k in sorted(accum)]
    merged = cards + l2_raw
    merged.sort(key=_rank_key)
    return merged


# ============================================================
# 粗排候选预算（L1 定稿，2026-10-30）：命中路径打分 + 截断
# ============================================================

async def _merge_semantic(session: AsyncSession, query: str,
                          accum: dict[int, dict], trace: dict[str, Any]) -> None:
    """命中路径的语义面：给已有候选打语义分，并补回实体倒排漏掉的卡。

    为什么命中路径也要跑：实测 86/88 轮走 hit（快表实体倒排命中），而原实现"命中即不回源"
    把语义路整条短路了——候选**没有任何相关性信号**，_rank_key 退化成按 id 新优先。
    代价是一次 embed + 一次 Qdrant 查询（亚秒级），换掉的是逐张 LLM 判档（本题实测 110 次）。
    """
    raw = await _semantic_raw(query, limit=_SEMANTIC_COARSE_TOPK)
    if not raw:
        trace["coarse"]["semantic"] = "unavailable"     # 降级：不截断（方向取"宁多勿漏"）
        return
    trace["coarse"]["semantic"] = "scored"
    score_of = dict(raw)
    for card_id, item in accum.items():                  # ① 给已有候选补分
        if card_id in score_of:
            item["score"] = score_of[card_id]
    added = 0
    for card_id, score in raw[:_SEMANTIC_TOPK]:          # ② 语义路独有召回（只取采信名次内的）
        if card_id in accum:
            continue
        hits, _ = pool.get_cards([card_id])
        card = hits[0] if hits else await store.get_card(session, card_id)
        if card is None or getattr(card, "status", store.ACTIVE) != store.ACTIVE:
            continue
        _accumulate(accum, card, {"semantic"}, score=score)
        added += 1
    trace["coarse"]["semantic_added"] = added


def _coarse_truncate(accum: dict[int, dict], keep: int, trace: dict[str, Any]) -> None:
    """按语义分截断候选集（**候选预算**，不是相关性筛选）。

    纪律三条：
    - 只在语义分可用时截断——没有相关性信号就宁可全判（fail-safe 方向 = 宁多勿漏）；
    - 被截断的卡仍在册、仍可经实体/时间/顺线等入口在别的轮次进入，不换出、不下架；
    - 精筛的判定口径完全不变，本函数只决定"它看多少张"。
    """
    total = len(accum)
    scored = any(item["score"] is not None for item in accum.values())
    if keep <= 0 or total <= keep or not scored:
        trace["coarse"].update({"total": total, "kept": total, "dropped": 0})
        return
    ordered = sorted(
        accum.items(),
        key=lambda kv: (kv[1]["score"] is None, -(kv[1]["score"] or 0.0), -kv[0]),
    )
    kept_items = dict(ordered[:keep])
    dropped = total - keep
    accum.clear()
    accum.update(kept_items)
    trace["coarse"].update({
        "total": total, "kept": keep, "dropped": dropped,
        "min_kept_score": round(min((v["score"] or 0.0) for v in kept_items.values()), 4),
    })


# ============================================================
# 主入口（分档调度）
# ============================================================

async def retrieve(query: str, *, session: AsyncSession,
                   now: datetime | None = None) -> RetrievalResult:
    """一次检索 = 一次缺页处理。

    - session：调用方（chat_pipeline）的 AsyncSession——本模块只读、不开事务；
    - now：时间词换算基准（可注入保证测试确定性；默认 UTC 当前时刻）。
    """
    candidates, trace = await _retrieve_full(query, session,
                                             now or datetime.now(timezone.utc))
    return RetrievalResult(candidates=candidates, trace=trace)


async def _retrieve_full(query: str, session: AsyncSession,
                         now: datetime) -> tuple[list[Candidate], dict]:
    """完整检索链：抽线索 → 扫快表 → （缺页）两级回源 → 回填 → 顺线 → 排序。

    分诊已在 L12 删除。它在真实对话上把「看起来像通用问题、其实指向用户自己」的查询
    判成 skip，直接关掉整条检索，模型随后用常识编答案——实测「记忆卡片写入成本」一题，
    正确证据语义分 0.846 排第一，却因为 skip 一张没进上下文。
    省下的 1.2 秒不值这个代价，而且链路一旦压到 2 秒量级，它的盈亏点会反转成「必删」。
    """
    trace = _empty_trace()
    t_start = time.perf_counter()

    # ① 抽线索（纯确定性；零 LLM、零 IO）
    entities, window = _extract_clues(query, now)
    trace["entities"] = entities
    if window is not None:
        trace["time_window"] = [window[0].isoformat(), window[1].isoformat()]

    accum: dict[int, dict] = {}
    l2_raw: list[Candidate] = []

    # ③ 扫快表（零 IO）；本地有收获 = 够格（hit），不回源
    entity_hits, missing, time_hits = _scan_pool(entities, window)
    trace["entities_missing"] = missing
    trace["pool_hits"] = {"entity": len(entity_hits), "time": len(time_hits)}
    direct_ids = sorted(set(entity_hits) | set(time_hits))
    trace["hit_cards"] = direct_ids
    if direct_ids:
        trace["triage"] = "hit"
        hits, _ = pool.get_cards(direct_ids)
        for card in hits:
            routes = set()
            if card.id in entity_hits:
                routes.add("entity")
            if card.id in time_hits:
                routes.add("time")
            _accumulate(accum, card, routes)
        # L1 定稿：命中不再短路语义路——语义分既是粗排依据，也是实体倒排漏卡的补充召回
        await _merge_semantic(session, query, accum, trace)
    else:
        # ④ 缺页回源：L1（三入口）→ 仍全空 → L2（原文兜底）
        trace["triage"] = "fault"
        l1_entity, l1_time, l1_semantic = await _source_l1(session, query, entities, window)
        trace["l1"] = {"entity": len(l1_entity), "time": len(l1_time),
                       "semantic": len(l1_semantic), "refilled": 0}
        for card in l1_entity:
            _accumulate(accum, card, {"entity"})
        for card in l1_time:
            _accumulate(accum, card, {"time"})
        for card, score in l1_semantic:
            _accumulate(accum, card, {"semantic"}, score=score)
        sourced = [accum[k]["card"] for k in sorted(accum)]
        if sourced:
            trace["fault_level"] = "L1"
            trace["l1"]["refilled"] = len(sourced)
            await _backfill(session, sourced)      # 回填：卡入池 + 恢复边（顺线立即可见）
        else:
            trace["fault_level"] = "L2"
            # L8：纯缺页场景下原文是唯一证据，预算放宽一倍
            l2_raw = await _merge_raw(session, query, entities, accum, trace,
                                      topk=settings.RETRIEVAL_RAW_TOPK * 2, min_hits=1)

    # ⑤ 粗排候选预算：按语义分截断（无相关性信号时不截断——方向取宁多勿漏）
    #    位置纪律：必须在顺线之前——顺线是"精确扩邻"，让它只在预算内的卡上跑。
    _coarse_truncate(accum, settings.RETRIEVAL_COARSE_TOPK, trace)

    # ⑥ 顺线一跳（只顺因果；池内免费 + 池外小预算预取）
    direct_cards = [accum[k]["card"] for k in sorted(accum)]
    await _line_expand(session, direct_cards, set(accum), accum, trace)

    # ⑦ 原文通道（L8）：命中路径与 L1 回源路径都补一次。
    #    纯缺页（L2）那支上面已经取过原文，不重复。
    if trace["fault_level"] != "L2":
        l2_raw = await _merge_raw(session, query, entities, accum, trace,
                                  topk=settings.RETRIEVAL_RAW_TOPK)

    # ⑧ 收口：候选 + 归因计数（每候选按最强入口归因，Σ = merged_count）+ trace
    candidates = _finalize(accum, l2_raw)
    counts = {"entity": 0, "semantic": 0, "time": 0, "line": 0, "raw": len(l2_raw)}
    for key in sorted(accum):
        best = min(accum[key]["routes"], key=lambda r: _ROUTE_RANK.get(r, 9))
        counts[best] += 1
    trace["candidates"] = counts
    trace["merged_count"] = len(candidates)
    trace["stage_ms"]["retrieval"] = round((time.perf_counter() - t_start) * 1000, 1)
    return candidates, trace
