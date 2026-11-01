"""LoCoMo 评测适配层（只做适配，不改毕设记忆逻辑）。

三条纪律：
1. 不修改 AI-Customer-Service 任何源文件——所有差异都通过**环境变量**（官方配置面）
   与**运行时属性注入**（monkeypatch，进程内、可审阅）表达；
2. 记忆逻辑（gate / extractor / consolidator / retriever / reranker / injector / store /
   writer / pool / journal / explicit / evictor）一行不动；唯一被替换的是**生成侧**的
   实验条件（system prompt 语言、抽取输出语言），因为 LoCoMo 是英文数据集；
3. 全部替换点集中在 `apply_language_patches()`，内容在文件里逐字可查。

用法约束：本模块必须在 import 任何 app.* 之前被 import（它负责先写环境变量）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# 路径自适应：bench/scripts/bench_lib.py → 仓库根 = parents[2]
PROJECT_DIR = Path(__file__).resolve().parents[2]
BENCH_DIR = Path(__file__).resolve().parents[1]
DATA_FILE = Path(os.environ.get("LOCOMO_PATH") or (BENCH_DIR / "data" / "locomo10.json"))
OUT_DIR = BENCH_DIR / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


# ============================================================
# 环境准备（必须在 import app.* 之前执行）
# ============================================================

def prepare_env(cond: str, *, window_size: int = 10, state: str | None = None) -> None:
    """为每个「样本 × 条件」切一套独立状态（库 / journal / 向量集合 / 埋点）。

    state 用于"复用同一份灌好的库、只换输出名"——改代码后重跑同一批题时必须复用库与
    向量集合（换 cond 会指到空集合，语义路直接失效），因此把状态与结果命名拆开。
    """
    state = state or cond
    (PROJECT_DIR / "data" / "bench").mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./data/bench/{state}.db"
    os.environ["EXTRACT_JOURNAL_PATH"] = f"data/bench/{state}.journal.log"
    os.environ["EVICT_LOG_PATH"] = f"data/bench/{state}.evict.jsonl"
    os.environ["EDIT_LOG_PATH"] = f"data/bench/{state}.edit.jsonl"
    os.environ["TELEMETRY_DIR"] = f"data/bench/telemetry_{cond}"
    os.environ["QDRANT_COLLECTION"] = f"locomo_{state}"
    os.environ["WINDOW_SIZE"] = str(window_size)
    os.environ["DEBUG_MODE"] = "False"
    # 淘汰规则在历史回放场景下必须关掉：LoCoMo 的对话发生在 2023，而系统时钟是 2026。
    # 目标卡按 event_time+30 天判过期 → 全灭；偏好卡按 0.5^(1240/90) ≈ 7e-5 衰减 → 全灭。
    # 这是"基准时间基准"与"系统当前时刻"的错位，不是系统缺陷，故回放时禁用（生产配置不变）。
    os.environ["EVICT_GOAL_GRACE_DAYS"] = "1000000"
    os.environ["EVICT_MIN_SCORE"] = "0"
    os.chdir(PROJECT_DIR)


# ============================================================
# 生成侧实验条件（英文对齐；记忆抽取跟随对话语言）
# ============================================================

# 答案侧 prompt（L4 定稿 2026-10-30）：原版把"不知道"设成默认出口，实测 31.8% 的
# 正常题直接拒答（记 0 分）。改动三处：
# ① 拒答从"默认"降级为"最后手段"——能推断就推断，措辞不同不算不知道；
# ② 列表题要求列全（实测丢分里有 3 题是只答了 4 项中的 2 项）；
# ③ 要求给具体值（人名/日期/数字）而不是概括。
ANSWER_SYSTEM_PROMPT = (
    "You are a personal assistant with long-term memory of past conversations. "
    "Below you may be given memories retrieved from long-term storage, and the most recent "
    "conversation turns. Each conversation line is prefixed with the speaker's name.\n"
    "Rules:\n"
    "- Answer in English, in a short phrase.\n"
    "- Output ONLY the answer: the value asked for. No explanation, no rationale, no preamble, "
    "no restating the question, no quotation marks.\n"
    "- If the question asks for several items, list ALL the items you can find, not just one.\n"
    "- The wording in the memories may differ from the wording of the question. Judge by meaning, "
    "not by exact match, and answer as long as the information is there in some form.\n"
    "- If the answer is not stated outright but follows from the facts present, infer it.\n"
    "- Never move a fact to a different person, place or date than the one it is attached to. "
    "If the question asks what a particular person did, and the memories show somebody else did "
    "it, output exactly: Not mentioned in the conversation.\n"
    "- If there is no related information at all, output exactly: Not mentioned in the conversation."
)

# 抽取器 prompt：与 extractor._EXTRACT_SYSTEM 逐条对应，只改两处
# ① 卡片正文语言跟随对话本身的语言（原版写死"中文一句话"）；
# ② 示例措辞不变，记忆逻辑（抽取标准 / 词表 / 校验）完全一致。
EXTRACT_SYSTEM_EN = """You are the memory extractor of a personal assistant. Extract the information worth remembering long-term from the conversation, and assemble it into memory cards.

Extraction criteria (better to omit than to over-extract — if unsure, do not extract):
- Only extract facts about the user that remain useful across sessions: identity (occupation/residence/family), preferences (likes/dislikes/habits), goals (plans/things in progress), relationships, important experiences (what happened).
- Do not extract: small talk, one-off Q&A, pleasantries, temporary states that mean nothing outside the current conversation.
- Each card is an independent, readable atomic fact that does not rely on contextual pronouns (resolve "he/there/this" into concrete content).
- Write list-type information item by item. Do not collapse four activities into "enjoys various
  activities" — name every activity, every place, in the card text.
- Each line starts with [YYYY-MM-DD], the date that utterance was made. Resolve relative time expressions
  (yesterday / last week / next month / that day) into absolute dates using that date and put the result in
  event_time. If it cannot be resolved, leave event_time null instead of guessing.
  The date prefix is only for your calculation. Never copy it into the card content.
- entity card (the user's attribute/state): kind="entity", must give subtype (identity/preference/goal/relation);
- event card (something that happened): kind="event", subtype is null; if the time can be inferred, fill event_time (ISO format).
- entities lists the people, places and objects involved (empty array if none).
- Write the card content in the same language the conversation itself is written in.

Also list the NODES — the people, places, organisations and objects worth remembering:
- one node per person or place, no duplicates; different names for the same person (Mel / Melanie)
  collapse into one node, with the other forms in aliases;
- type must be exactly one of: person / place / org / thing;
- description is one sentence saying what it is and how it relates to the user.
- nodes are the anchors that events point to. No nodes means no "who" and no "where".

Output only one json object, no other text. Format:
{"nodes": [{"name": "canonical name", "type": "person/place/org/thing",
            "aliases": ["..."], "description": "one sentence"}],
 "cards": [{"kind": "entity or event", "subtype": "identity/preference/goal/relation or null",
            "content": "card text (one sentence)", "entities": ["..."], "event_time": "ISO time or null"}]}
When there is nothing worth remembering, output {"nodes": [], "cards": []}"""


def apply_language_patches() -> None:
    """替换生成侧的两个 prompt（进程内属性注入；源文件不动）。"""
    import app.core.short_term_memory as stm
    import app.services.long_term_memory.extractor as extractor

    stm.SYSTEM_PROMPT = ANSWER_SYSTEM_PROMPT          # assemble_package 运行时读模块全局
    extractor._EXTRACT_SYSTEM = EXTRACT_SYSTEM_EN     # _call_model 运行时读模块全局


# ============================================================
# 观测（spy）：不改变行为，只截获中间产物
# ============================================================

@dataclass
class Capture:
    """一轮问答的中间产物（读路径原文 + 三段 trace + token）。"""

    memory_text: str | None = None
    retrieval_trace: dict[str, Any] = field(default_factory=dict)
    rerank_trace: dict[str, Any] = field(default_factory=dict)
    injector_trace: dict[str, Any] = field(default_factory=dict)
    kept: list[Any] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reply: str = ""


_CAP = Capture()
_PATCHED = {"spy": False, "writeoff": False}


def install_spies() -> None:
    """给读路径打观测点（包装原函数，调用顺序与返回值完全不变）。"""
    if _PATCHED["spy"]:
        return
    import app.services.chat_pipeline as cp

    orig_retrieve = cp._retrieve_memory_context
    orig_gen = cp.generation_service.generate_reply

    async def spy_retrieve(*args, **kwargs):
        res = await orig_retrieve(*args, **kwargs)
        _CAP.memory_text = res.text
        _CAP.retrieval_trace = res.retrieval_trace
        _CAP.rerank_trace = res.rerank_trace
        _CAP.injector_trace = res.injector_trace
        _CAP.kept = res.kept
        return res

    async def spy_gen(package):
        res = await orig_gen(package)
        _CAP.prompt_tokens = res.prompt_tokens
        _CAP.completion_tokens = res.completion_tokens
        _CAP.total_tokens = res.total_tokens
        _CAP.reply = res.reply
        return res

    cp._retrieve_memory_context = spy_retrieve
    cp.generation_service.generate_reply = spy_gen
    _PATCHED["spy"] = True


def enable_writeoff() -> None:
    """评测提问阶段关闭**写回**（只关评测提问的副作用，读路径一切照旧）。

    为什么必须关：LoCoMo 的 199 个提问不属于被测对话。若照常写回，
    第 3 题就会召回第 1 题的问答记录，形成跨题污染与信息泄漏。
    关掉的是两处副作用：messages 归档（save_record_to_db）与写路径触发（note_activity）。
    """
    if _PATCHED["writeoff"]:
        return
    import app.services.chat_pipeline as cp
    from app.services.long_term_memory import consolidator

    async def _noop(*args, **kwargs):
        return None

    cp.save_record_to_db = _noop
    consolidator.note_activity = _noop
    _PATCHED["writeoff"] = True


def reset_capture() -> None:
    global _CAP
    _CAP = Capture()


def capture() -> Capture:
    return _CAP


# ============================================================
# 数据读取
# ============================================================

def load_locomo() -> list[dict]:
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def session_keys(sample: dict) -> list[str]:
    conv = sample["conversation"]
    keys = [k for k in conv if re.fullmatch(r"session_\d+", k)]
    return sorted(keys, key=lambda k: int(k.split("_")[1]))


def flatten_conversation(sample: dict) -> list[dict]:
    """按会话顺序摊平为消息列表（speaker / text / dia_id / session）。

    每条消息的正文加说话人前缀（`Caroline: ...`）——系统是单用户助手，
    两人对话按"用户转述"形态入库，说话人归属留在正文里（LoCoMo 适配的通行做法）。
    """
    out: list[dict] = []
    for key in session_keys(sample):
        for turn in sample["conversation"][key]:
            out.append({
                "session": key,
                "session_date": sample["conversation"].get(f"{key}_date_time"),
                "speaker": turn["speaker"],
                "dia_id": turn.get("dia_id"),
                "text": turn["text"],
                "content": f'{turn["speaker"]}: {turn["text"]}',
            })
    return out


def qa_list(sample: dict) -> list[dict]:
    """QA 列表：category 5（adversarial）没有 answer，单列出来。"""
    out = []
    for i, q in enumerate(sample["qa"]):
        item = dict(q)
        item["qid"] = i
        item["is_adversarial"] = q.get("category") == 5
        out.append(item)
    return out


def gold_answer(item: dict) -> str:
    ans = item.get("answer", item.get("adversarial_answer", ""))
    if ans is None:
        return ""
    if isinstance(ans, list):
        return ", ".join(str(x) for x in ans)
    return str(ans)


def evidence_texts(sample: dict, evidence: list[str]) -> list[str]:
    """evidence 里的 dia_id（D1:3）→ 对应发言原文。"""
    turn_by_dia = {m["dia_id"]: m["text"] for m in flatten_conversation(sample)}
    return [turn_by_dia[e] for e in evidence if e in turn_by_dia]


# ============================================================
# 向量库清理（每个条件用独立集合，结束时删掉）
# ============================================================

def drop_collection(collection: str) -> None:
    from qdrant_client import QdrantClient

    from app.core.config import settings
    client = QdrantClient(url=settings.QDRANT_URL, timeout=10)
    if client.collection_exists(collection):
        client.delete_collection(collection)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
