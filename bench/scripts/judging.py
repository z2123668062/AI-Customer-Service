"""判定规则（本文件即"评分口径"本身，所有阈值与措辞在此可查）。

三件事：
1. `token_f1`：LoCoMo 官方口径的 token 级 F1（同时给 F1>=0.5 的比例，防多短语答案被 F1 压死）；
2. `judge_correct`：LLM 评审（问题 + 标准答案 + 模型回答 → CORRECT / WRONG），主口径；
3. `judge_adversarial`：诱饵题三分类（HALLUCINATION / REFUSAL / OTHER），
   规则优先（诱饵关键词命中即幻觉），规则不吃判再交 LLM。

所有判定函数都是纯"事后"的，不碰记忆系统。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import settings

client = AsyncOpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
JUDGE_MODEL = settings.LLM_MODEL

# ============================================================
# 文本归一与 token F1（LoCoMo / SQuAD 口径）
# ============================================================

_PUNC = set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')
_ARTICLES = {"a", "an", "the"}


def normalize_answer(s: str) -> str:
    s = (s or "").lower()
    s = "".join(ch if ch not in _PUNC else " " for ch in s)
    tokens = [t for t in s.split() if t and t not in _ARTICLES]
    return " ".join(tokens)


def _tokens(s: str) -> list[str]:
    return normalize_answer(s).split()


def token_f1(pred: str, gold: str) -> tuple[float, float, float]:
    """返回 (f1, precision, recall)。空预测 → 全 0；空 gold → 0。"""
    p, g = _tokens(pred), _tokens(gold)
    if not p or not g:
        return 0.0, 0.0, 0.0
    from collections import Counter

    common = Counter(p) & Counter(g)
    same = sum(common.values())
    if same == 0:
        return 0.0, 0.0, 0.0
    precision = same / len(p)
    recall = same / len(g)
    return 2 * precision * recall / (precision + recall), precision, recall


# ============================================================
# 正常题：LLM 评审
# ============================================================

_CORRECT_SYSTEM = """You are grading an assistant's answer on a long-conversation memory benchmark.

You are given: the question, the reference (gold) answer, and the assistant's answer.

Decide whether the assistant's answer is CORRECT or WRONG.

Rules:
- CORRECT: the assistant answer conveys the same key information as the reference answer.
  Formatting differences are irrelevant: "7 May 2023" == "May 7, 2023"; "4 years" == "four years";
  "Mel" == "Melanie"; "not" vs "n't" etc.
- RELATIVE TIME RULE (strict): when the reference answer gives an absolute date or a calendar anchor
  ("2022", "7 May 2023", "the week before 9 June 2023"), a relative expression in the assistant answer
  ("last week", "yesterday", "next month", "last year", "a recent Saturday",
  "two days before this conversation", "last Friday") is NOT equivalent to it. Mere repetition of the
  phrasing used inside the conversation does not answer "when" — mark it WRONG.
- For a reference answer that is a LIST of items: CORRECT if the assistant names nearly all of the
  items (at least two thirds) and adds nothing wrong; WRONG if it names only one or two items of a
  longer list, or adds items that are not in the reference and not supported.
- WRONG: the assistant answer is empty, is a refusal ("not mentioned", "I don't know",
  "the conversation does not say"), contradicts the reference, or is so vague that a human grader
  would not accept it as the same answer.
- Extra polite wording, hedging, or restating the question around a correct answer is still CORRECT.

Output only one json object, no other text:
{"label": "CORRECT" or "WRONG", "reason": "one short sentence"}"""


@dataclass
class Verdict:
    label: str
    reason: str
    origin: str = "llm"


async def judge_correct(question: str, gold: str, pred: str) -> Verdict:
    """正常题判定（严格口径）：CORRECT / WRONG。调用失败 → 抛（由上层记 error，不静默当错）。"""
    user = f"Question: {question}\nReference answer: {gold}\nAssistant answer: {pred}"
    raw = await _call(_CORRECT_SYSTEM, user)
    obj = _load(raw)
    label = str(obj.get("label", "")).strip().upper()
    if label not in ("CORRECT", "WRONG"):
        raise ValueError(f"判定输出非法: {raw[:200]!r}")
    return Verdict(label, str(obj.get("reason", ""))[:300])


# ============================================================
# 宽松口径（对齐 Mem0 LoCoMo 协议）：双口径报告用
# ============================================================

_LENIENT_SYSTEM = (
    "You are grading an assistant's answer on a long-conversation memory benchmark.\n"
    "Be GENEROUS with your grading, following the Mem0 LoCoMo protocol:\n"
    "- As long as the generated answer touches on the same topic as the gold answer, "
    "count it CORRECT.\n"
    "- Do not require exact wording, exact numbers, or full coverage of a list.\n"
    "- Count it WRONG only if the answer is unrelated, empty, or explicitly refuses.\n"
    'Output only one json object, no other text: '
    '{"label": "CORRECT" or "WRONG", "reason": "one short sentence"}'
)


async def judge_lenient(question: str, gold: str, pred: str) -> Verdict:
    """宽松口径判定：与 judge_correct 同输入，只换尺子。用于和文献数字对表。

    注意：这是对 Mem0 协议精神的复现（沾边算对），不是它的原始 prompt，
    reader 也不是 gpt-4o-mini。所以只能横向比量级，不能当榜单分数。
    """
    user = f"Question: {question}\nReference answer: {gold}\nAssistant answer: {pred}"
    raw = await _call(_LENIENT_SYSTEM, user)
    obj = _load(raw)
    label = str(obj.get("label", "")).strip().upper()
    if label not in ("CORRECT", "WRONG"):
        raise ValueError(f"宽松判定输出非法: {raw[:200]!r}")
    return Verdict(label, str(obj.get("reason", ""))[:300], origin="lenient")


# ============================================================
# 诱饵题：幻觉 / 拒答 / 其他
# ============================================================

_REFUSAL_MARKERS = (
    "not mentioned", "no mention", "not specified", "not stated", "not provided",
    "not given", "not discussed", "not in the conversation", "does not say", "doesn't say",
    "did not say", "didn't say", "not clear from", "no information", "i don't know",
    "i do not know", "unknown", "cannot be determined", "can't be determined",
    "not enough information", "there is no information", "the conversation does not",
    "unable to determine", "n/a",
)

_ADV_SYSTEM = """You are auditing a memory assistant for one specific failure: swallowing a false premise.

A question is asked about a conversation. The question presupposes something that is not true in
the conversation. The "decoy" below is the plausible-but-ungrounded answer that a system which
blindly trusts the premise would give.

Classify the assistant's answer into exactly one of three labels:

- HALLUCINATION: the answer asserts the decoy, or a paraphrase of it, as the answer to the
  question. This is the failure being measured.
- REFUSAL: the answer declines (says the information is not mentioned / not known / cannot be
  determined), OR it denies the premise (answers "No", "that was not X", "actually it was Y")
  without restating the decoy. Denying a false premise is correct behaviour, not a hallucination.
- OTHER: anything else, including a different concrete answer that neither repeats the decoy nor
  denies the premise.

Do not upgrade a premise denial to HALLUCINATION just because the answer contains a concrete noun.
Judge whether the decoy's claim itself is being asserted.

Output only one json object, no other text:
{"label": "HALLUCINATION" or "REFUSAL" or "OTHER", "reason": "one short sentence"}"""

_DECOY_STOP = set(
    "a an the is are was were be been being of to in on at for with and or but that this these "
    "those it its he she they them his her their i you we my your our about after before "
    "important because from as by into over under".split()
)


def _content_words(text: str) -> list[str]:
    return [w for w in _tokens(text) if w not in _DECOY_STOP and len(w) > 1]


def decoy_hit(decoy: str, pred: str) -> bool:
    """规则：诱饵答案的实词是否基本被回答复述（≥60% 且至少 2 个词重合）。"""
    d, p = set(_content_words(decoy)), set(_content_words(pred))
    if not d:
        return False
    inter = d & p
    return len(inter) >= 2 and len(inter) / len(d) >= 0.6


def looks_refusal(pred: str) -> bool:
    t = (pred or "").lower()
    return any(m in t for m in _REFUSAL_MARKERS)


# 否定错误前提（L8）：诱饵题里模型答「No, Melanie made it.」是在纠正 premise，
# 不是幻觉。原实现把这种答法记成 HALLUCINATION，把拒答率压低了近一倍。
_NEGATION_RE = re.compile(r"^\s*(no\b|nope\b|not\b|never\b|none\b|actually\b|incorrect\b|"
                          r"it\s+(is|was)\s+not\b|it's\s+not\b|that's\s+not\b)", re.IGNORECASE)


def denies_premise(pred: str) -> bool:
    return bool(_NEGATION_RE.match((pred or "").strip()))


async def judge_adversarial(question: str, decoy: str, pred: str) -> Verdict:
    """诱饵题判定：规则优先（关键词命中=幻觉、明确说不知道=拒答），规则不吃判交 LLM。"""
    if decoy_hit(decoy, pred):
        return Verdict("HALLUCINATION", f"规则命中：回答复述了诱饵「{decoy}」", origin="rule")
    if looks_refusal(pred):
        return Verdict("REFUSAL", "规则命中：回答含明确的「不知道/未提及」表述", origin="rule")
    if denies_premise(pred):
        return Verdict("REFUSAL", f"规则命中：回答否定了问题前提「{decoy}」", origin="rule")
    user = f"Question: {question}\nDecoy (ungrounded) answer: {decoy}\nAssistant answer: {pred}"
    obj = _load(await _call(_ADV_SYSTEM, user))
    label = str(obj.get("label", "")).strip().upper()
    if label not in ("HALLUCINATION", "REFUSAL", "OTHER"):
        raise ValueError(f"诱饵判定输出非法: {obj!r}")
    return Verdict(label, str(obj.get("reason", ""))[:300])


# ============================================================
# 上下文命中（辅助指标）：标准证据原文是否出现在模型看到的上下文里
# ============================================================

def context_hit(evidence_texts: list[str], context: str) -> bool:
    """命中定义（自定义口径，非官方）：某条标准证据的实词有 ≥70% 且至少 3 个词出现在上下文里。

    上下文 = 注入的记忆段 + 短记忆窗口原文。阈值刻意收紧：证据句与相邻寒暄共享常用词，
    50% 的重合几乎恒成立（会把命中率虚高到 50%+），70% 才真正等价于"这句话被看到了"。
    """
    ctx = set(_content_words(context or ""))
    if not ctx:
        return False
    for ev in evidence_texts:
        words = set(_content_words(ev))
        if len(words) < 3:
            continue
        inter = words & ctx
        if len(inter) >= 3 and len(inter) / len(words) >= 0.7:
            return True
    return False


# ============================================================
# 内部工具
# ============================================================

async def _call(system: str, user: str) -> str:
    resp = await client.chat.completions.create(
        model=JUDGE_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""


def _load(raw: str) -> dict:
    t = raw.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t[3:]
        t = t.rstrip("`").rstrip()
    try:
        return json.loads(t)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"判定输出不是 json: {raw[:200]!r}") from exc
