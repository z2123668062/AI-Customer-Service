# app/services/long_term_memory/extractor.py
"""写路径 · 抽取器：消息批次 → 结构化卡片草稿（CardDraft）——只抽不管。

职责单一：读消息、调 LLM、产出**草稿**；不做归位/去重/落库（writer 的事）。
两通道共用一个抽取器：
- 被动通道 extract_batch(messages)：consolidator 按水位取批调用；
- 显式通道 extract_explicit(entries)：显式消息单独优先抽取（writer 落库时打 source=explicit）。

降级纪律（与 gate 同风格——"抽取挂了系统照转"）：
- 批次分段抽取（每段 _SEGMENT_SIZE 条）；某段失败 → 丢弃该段（warn），其余段继续；
- 全部段失败 → 返回 None（调用方不推进水位 = 整批重试，宁重勿漏）；
- 部分段失败 → 返回成功段产出（水位正常推进；失败段消息可经 messages 重抽，宁缺不重）。

模型调用模式与 gate 一致：约束输出（json_object）失败 → 纯 prompt 重试 1 次 → 判定为失败。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Sequence

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import logger
from app.models.domain import CARD_SUBTYPES_ATTR, CARD_SUBTYPES_NODE, Message

# ============ 模型调用（约束模式；模式与 gate 一致）============

client = AsyncOpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)

_TIMEOUT_SECONDS = 30.0   # 后台任务宽松超时（非热路径；不是实验变量）
_SEGMENT_SIZE = 30        # 单次 LLM 调用的消息条数上限（防批过大）
_CONTENT_MAX = 500        # 单卡正文长度上限（超长丢弃——话痨输出不可信）
_MAX_ENTITIES = 10        # 单卡实体数上限
_NODE_NAME_MAX = 60       # 节点标准名长度上限（防模型把整句话当名字）
_MAX_ALIASES = 5          # 单节点别名数上限

_EXTRACT_SYSTEM = """你是个人助手的记忆抽取器。从对话记录中抽取值得长期记住的信息，组装成记忆卡片。

抽取标准（宁缺勿滥——没把握就不抽）：
- 只抽关于【用户】的、跨越会话仍然有用的事实：身份（职业/居住/家庭）、偏好（喜欢/讨厌/习惯）、
  目标（计划/正在做的事）、人际关系、重要经历（发生了什么）。
- 不抽：寒暄闲聊、一次性问答、助手说的话、离开当前对话就没有意义的临时状态。
- 每张卡是独立可读的原子事实，不依赖上下文代词（把“他/那里/这个”还原成具体内容）。
- 列表型的信息要逐项写出，不要概括成“喜欢各种活动”——把每个活动、每个地点都写进正文
  （L3：实测列表题丢分集中在只答对一半，而这种损失在检索阶段补不回来）。
- 每行开头的 [YYYY-MM-DD] 是该条发言的发生日期（L3）：遇到“昨天/上周/下个月/那天”这类相对说法，
  按该日期换算成绝对日期填入 event_time（换算不出来就留 null，不要猜）。
  **日期前缀只是给你换算用的，绝对不要写进 content 正文。**
- entity 卡（用户的属性/状态）：kind="entity"，必须给 subtype（identity/preference/goal/relation）；
- event 卡（发生过的事件）：kind="event"，subtype 为 null；能推断发生时间就填 event_time（ISO 格式）。
- entities 列出涉及的人、地、物名称（没有就空数组）。

另外单独列出**节点**（nodes）——值得长期记住的人、地点、组织、物品：
- 每个人、每处地点只出现一次；同一个人不同叫法（Mel / Melanie）合成一个节点，别名写进 aliases；
- type 只能取 person / place / org / thing 四者之一；
- description 一句话说清它是什么、和用户什么关系。
- 节点是事件可以指向的锚点，没有节点就没有「谁」「哪里」这两条线索。

只输出一个 json 对象，不要输出任何其他文字。格式：
{"nodes": [{"name": "标准名", "type": "person/place/org/thing",
            "aliases": ["别名"], "description": "一句话"}],
 "cards": [{"kind": "entity 或 event", "subtype": "identity/preference/goal/relation 或 null",
            "content": "卡片正文（一句话）", "entities": ["..."], "event_time": "ISO时间或null"}]}
没有值得记的内容时输出 {"nodes": [], "cards": []}"""

_EXPLICIT_HINT = ("注意：以下是用户**明确要求记住**的内容——务必尽力抽取其中值得长期保留的信息。\n\n")

# 时间戳前缀（L3）：消息发生日期由 _render_messages 写在每行开头，供相对时间换算。
# 模型偶尔会把前缀抄进卡片正文——那是"消息时间"不是"事实时间"，抄进正文会污染注入段，
# 所以校验时无条件剥掉（纯清洗，不改语义）。
_DATE_PREFIX_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}\]\s*")


@dataclass(frozen=True, slots=True)
class CardDraft:
    """抽取草稿（writer 的输入单元；字段与 store.create_card 参数一一对应）。"""

    kind: str                              # "entity" / "event"（task 不收）
    content: str                           # 卡片正文（原子事实、独立可读）
    subtype: str | None = None             # 属性卡：identity/preference/goal/relation；节点卡：person/place/org/thing；event：None
    entities: tuple[str, ...] = ()         # 涉及的人地物（tuple 保持不可变）
    event_time: datetime | None = None     # 事件时间（解析失败即置 None——丢时间不丢卡）
    canonical_name: str | None = None      # L7：节点卡的标准名；属性卡与事件卡为 None（= 不是节点）
    source_message_ids: tuple[int, ...] = ()   # L7：来源原文 id，注入时可无损回带原文


def _strip_fence(text: str) -> str:
    """剥离 markdown 代码围栏（模型偶发包装；与 gate 同口径）。"""
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t[3:]
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3].rstrip()
    return t


# 模型偶尔不按 ISO 输出（"June 2, 2023" / "2 June 2023"），原实现会静默丢时间。
# L3 起按顺序兜底尝试；全失败才返回 None（丢时间不丢卡，方向不变）。
_TIME_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y",
                 "%d %b %Y", "%b %d, %Y", "%Y-%m")


def _parse_event_time(value: Any) -> datetime | None:
    """event_time 解析（ISO 优先，常见自然语言日期兜底；全失败返回 None）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _validate_card(raw: Any) -> CardDraft | None:
    """逐卡校验（过不了就丢卡——“宁可不记，不可记错”的写入侧体现）。"""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in ("entity", "event"):
        return None
    content = _DATE_PREFIX_RE.sub("", str(raw.get("content") or "")).strip()
    if not content or len(content) > _CONTENT_MAX:
        return None
    subtype = raw.get("subtype")
    subtype = str(subtype).strip().lower() if subtype else None
    if kind == "entity":
        if subtype not in CARD_SUBTYPES_ATTR:       # 词表外 = 丢弃（节点类型走 _validate_node，不从这里进）
            return None
    else:
        subtype = None                              # event 卡不带 subtype
    entities_raw = raw.get("entities") or []
    entities: list[str] = []
    if isinstance(entities_raw, list):
        for item in entities_raw:
            name = str(item).strip()
            if name and name not in entities:
                entities.append(name)
            if len(entities) >= _MAX_ENTITIES:
                break
    return CardDraft(kind=kind, content=content, subtype=subtype,
                     entities=tuple(entities), event_time=_parse_event_time(raw.get("event_time")))


def _validate_node(raw: Any) -> CardDraft | None:
    """节点草稿校验（L7）：人/地点/组织/物品 → 一张带 canonical_name 的实体卡。

    节点是 led_to 的端点。没有它，事件卡里的"谁""哪里"永远只是字符串，连不成边。
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    node_type = str(raw.get("type") or "").strip().lower()
    if not name or len(name) > _NODE_NAME_MAX or node_type not in CARD_SUBTYPES_NODE:
        return None
    desc = str(raw.get("description") or "").strip()
    content = (desc or name)[:_CONTENT_MAX]
    aliases = [str(a).strip() for a in (raw.get("aliases") or [])
               if isinstance(a, str) and str(a).strip() and str(a).strip() != name]
    # entities 里放「标准名 + 别名」：快表的实体倒排据此建索引，问 Mel 也能命中 Melanie 这张卡
    entities = tuple(dict.fromkeys([name, *aliases[:_MAX_ALIASES]]))
    return CardDraft(kind="entity", content=content, subtype=node_type,
                     entities=entities, canonical_name=name)


def _parse_cards(raw: str) -> list[CardDraft] | None:
    """解析 LLM 输出 → 草稿列表；格式非法返回 None（触发重试）。空列表 = 合法结果（没信息可抽）。

    顺序纪律（L7）：节点排在卡片前面——写入时先有节点，事件卡的 led_to 才有端点可指。
    """
    try:
        obj = json.loads(_strip_fence(raw))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("cards"), list):
        return None
    nodes: list[CardDraft] = []
    for item in obj.get("nodes") or []:
        node = _validate_node(item)
        if node is not None:
            nodes.append(node)
        else:
            logger.debug(f"[extractor] 丢弃非法节点草稿: {item!r}")
    cards: list[CardDraft] = []
    for item in obj["cards"]:
        draft = _validate_card(item)
        if draft is not None:
            cards.append(draft)
        else:
            logger.debug(f"[extractor] 丢弃非法卡片草稿: {item!r}")
    return nodes + cards


def _stamp(created_at: Any) -> str:
    """消息时间戳前缀（[YYYY-MM-DD]）；缺失/异常 → 空前缀（丢时间不丢摄取）。"""
    if created_at is None:
        return ""
    if isinstance(created_at, str):
        return f"[{created_at[:10]}] "
    try:
        return f"[{created_at:%Y-%m-%d}] "
    except (TypeError, ValueError):
        return ""


def _render_messages(messages: Sequence[Message]) -> str:
    """渲染抽取输入。L3（2026-10-30）：带上消息发生日期——时间轴与事件时间的唯一信息来源。

    原实现只给「角色：正文」，相对时间（昨天/上周）无从换算，event_time 全为空，
    时间入口（pool 的时间轴）在真实对话上永远是空转。
    """
    lines = [f"{_stamp(m.created_at)}{'用户' if m.role == 'user' else '助手'}：{m.content}"
             for m in messages]
    return "对话记录：\n" + "\n".join(lines)


def _render_entries(entries: Sequence[dict[str, Any]]) -> str:
    lines = [f"用户：{e.get('content', '')}" for e in entries]
    return "对话记录：\n" + "\n".join(lines)


async def _call_model(user_content: str, *, json_mode: bool) -> str:
    """单次约束模式调用，返回原始文本；异常向上抛（由 _extract_once 接管重试）。"""
    kwargs: dict = dict(model=settings.EXTRACT_MODEL or settings.LLM_MODEL,
                        messages=[{"role": "system", "content": _EXTRACT_SYSTEM},
                                  {"role": "user", "content": user_content}],
                        temperature=0.0, top_p=0.1, timeout=_TIMEOUT_SECONDS)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def _extract_once(user_content: str) -> list[CardDraft] | None:
    """单次抽取（含重试 1 次纯 prompt）；全部失败返回 None。"""
    for attempt, json_mode in ((1, True), (2, False)):
        try:
            raw = await _call_model(user_content, json_mode=json_mode)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[extractor] 第 {attempt} 次调用异常：{type(e).__name__}: {e}")
            continue
        cards = _parse_cards(raw)
        if cards is not None:
            return cards
        logger.debug(f"[extractor] 第 {attempt} 次输出未过解析")
    return None


# ============ 对外接口 ============

async def extract_batch(messages: Sequence[Message]) -> list[CardDraft] | None:
    """被动抽取：消息批 → 草稿列表。返回 None = 全批失败（调用方不推进水位，整批重试）。"""
    if not messages:
        return []
    drafts: list[CardDraft] = []
    failed = 0
    segments = [messages[i:i + _SEGMENT_SIZE] for i in range(0, len(messages), _SEGMENT_SIZE)]
    for idx, seg in enumerate(segments):
        result = await _extract_once(_render_messages(seg))
        if result is None:
            failed += 1
            logger.warning(f"[extractor] 段 {idx + 1}/{len(segments)} 抽取失败（丢弃该段）")
        else:
            # L7：记下这一段的原文 id 作为来源。粒度是段（30 条），不是逐卡精确对应——
            # 抽取器给不出"这张卡来自哪一句"，而粗粒度来源已经足够注入时回带原文。
            ids = tuple(m.id for m in seg if getattr(m, "id", None) is not None)
            drafts.extend(replace(d, source_message_ids=ids) if ids else d for d in result)
    if failed == len(segments):
        return None
    return drafts


async def extract_explicit(entries: Sequence[dict[str, Any]]) -> list[CardDraft] | None:
    """显式通道抽取：待优先消息 → 草稿列表（None = 失败，调用方保留缓存重试）。"""
    if not entries:
        return []
    drafts = await _extract_once(_EXPLICIT_HINT + _render_entries(entries))
    ids = tuple(int(e["message_id"]) for e in entries if e.get("message_id") is not None)
    if drafts is None or not ids:
        return drafts
    return [replace(d, source_message_ids=ids) for d in drafts]


_PUNCT_RE = re.compile(r"[\s，。！？、,.!?;；:：\"'“”‘’()（）\-—~～]+")


def _normalize(text: str) -> str:
    """归一化正文（去空白与常见标点、统一小写）——批内去重键。"""
    return _PUNCT_RE.sub("", text).lower()


def dedupe_drafts(drafts: Sequence[CardDraft]) -> list[CardDraft]:
    """批内去重（归一化正文相同视为重复；保序保留首个）。"""
    seen: set[str] = set()
    kept: list[CardDraft] = []
    for d in drafts:
        key = _normalize(d.content)
        if key in seen:
            continue
        seen.add(key)
        kept.append(d)
    return kept
