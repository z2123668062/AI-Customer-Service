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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import logger
from app.models.domain import CARD_SUBTYPES_ENTITY, Message

# ============ 模型调用（约束模式；模式与 gate 一致）============

client = AsyncOpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)

_TIMEOUT_SECONDS = 30.0   # 后台任务宽松超时（非热路径；不是实验变量）
_SEGMENT_SIZE = 30        # 单次 LLM 调用的消息条数上限（防批过大）
_CONTENT_MAX = 500        # 单卡正文长度上限（超长丢弃——话痨输出不可信）
_MAX_ENTITIES = 10        # 单卡实体数上限

_EXTRACT_SYSTEM = """你是个人助手的记忆抽取器。从对话记录中抽取值得长期记住的信息，组装成记忆卡片。

抽取标准（宁缺勿滥——没把握就不抽）：
- 只抽关于【用户】的、跨越会话仍然有用的事实：身份（职业/居住/家庭）、偏好（喜欢/讨厌/习惯）、
  目标（计划/正在做的事）、人际关系、重要经历（发生了什么）。
- 不抽：寒暄闲聊、一次性问答、助手说的话、离开当前对话就没有意义的临时状态。
- 每张卡是独立可读的原子事实，不依赖上下文代词（把“他/那里/这个”还原成具体内容）。
- entity 卡（用户的属性/状态）：kind="entity"，必须给 subtype（identity/preference/goal/relation）；
- event 卡（发生过的事件）：kind="event"，subtype 为 null；能推断发生时间就填 event_time（ISO 格式）。
- entities 列出涉及的人、地、物名称（没有就空数组）。

只输出一个 json 对象，不要输出任何其他文字。格式：
{"cards": [{"kind": "entity 或 event", "subtype": "identity/preference/goal/relation 或 null",
  "content": "卡片正文（中文一句话）", "entities": ["..."], "event_time": "ISO时间或null"}]}
没有值得记的内容时输出 {"cards": []}"""

_EXPLICIT_HINT = ("注意：以下是用户**明确要求记住**的内容——务必尽力抽取其中值得长期保留的信息。\n\n")


@dataclass(frozen=True, slots=True)
class CardDraft:
    """抽取草稿（writer 的输入单元；字段与 store.create_card 参数一一对应）。"""

    kind: str                              # "entity" / "event"（task 不收）
    content: str                           # 卡片正文（原子事实、独立可读）
    subtype: str | None = None             # entity：identity/preference/goal/relation；event：None
    entities: tuple[str, ...] = ()         # 涉及的人地物（tuple 保持不可变）
    event_time: datetime | None = None     # 事件时间（解析失败即置 None——丢时间不丢卡）


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


def _parse_event_time(value: Any) -> datetime | None:
    """event_time 解析（ISO 字符串 → datetime；失败返回 None）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _validate_card(raw: Any) -> CardDraft | None:
    """逐卡校验（过不了就丢卡——“宁可不记，不可记错”的写入侧体现）。"""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in ("entity", "event"):
        return None
    content = str(raw.get("content") or "").strip()
    if not content or len(content) > _CONTENT_MAX:
        return None
    subtype = raw.get("subtype")
    subtype = str(subtype).strip().lower() if subtype else None
    if kind == "entity":
        if subtype not in CARD_SUBTYPES_ENTITY:     # 词表外 = 丢弃
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


def _parse_cards(raw: str) -> list[CardDraft] | None:
    """解析 LLM 输出 → 草稿列表；格式非法返回 None（触发重试）。空列表 = 合法结果（没信息可抽）。"""
    try:
        obj = json.loads(_strip_fence(raw))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("cards"), list):
        return None
    drafts: list[CardDraft] = []
    for item in obj["cards"]:
        draft = _validate_card(item)
        if draft is not None:
            drafts.append(draft)
        else:
            logger.debug(f"[extractor] 丢弃非法卡片草稿: {item!r}")
    return drafts


def _render_messages(messages: Sequence[Message]) -> str:
    lines = [f"{'用户' if m.role == 'user' else '助手'}：{m.content}" for m in messages]
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
            drafts.extend(result)
    if failed == len(segments):
        return None
    return drafts


async def extract_explicit(entries: Sequence[dict[str, Any]]) -> list[CardDraft] | None:
    """显式通道抽取：待优先消息 → 草稿列表（None = 失败，调用方保留缓存重试）。"""
    if not entries:
        return []
    return await _extract_once(_EXPLICIT_HINT + _render_entries(entries))


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
