# app/services/long_term_memory/gate.py
"""判定器：全部判断题的单一出口（E7 交付；系统设计.md §6；决策记录 D19/D21）。

对外只暴露五个判定函数（缺页 / 取代 / 回填 / 精筛 / 归属）——调用方只依赖函数签名与
GateResult 契约，不感知内部实现与降级逻辑（硬约束 ⑦：判定器是全部判断题的单一出口）。

分档（D19 级联纪律；约束模式经三轮 spike 实测定案，P50≈0.9~1.2s）：
    确定性前置（高置信正则短路，仅缺页，宁多勿漏）
    → deepseek-flash 约束判定（三层校验；失败重试 1 次 = 纯 prompt 模式）
    → 更强模型升级档（GATE_UPGRADE_MODEL，可选）
    → fail-safe 默认方向（工程规范 §3 方向表）。

纪律（铁律，改动须有测试覆盖）：
- 绝不向调用方抛异常：任何失败（超时 / 网络 / 解析 / 校验）都按 fail-safe 默认方向返回——
  "判定器挂了系统照转"（工程规范 §7：降级路径必须有测试）；
- 低置信（< GATE_CONFIDENCE_MIN）不采信模型结论，按保守方向处理；
- 误差方向：宁误判"要回忆"（有精筛兜底），不可漏判（漏判 = 失忆）；
- 日期比较 / 计数等确定性逻辑不进来（由代码算，工程规范 §3）；
- GATE_ENABLED=False → 全程旁路：五函数恒返回 fail-safe（B1/B2/B4 等消融档位）；
- 实验期冻结：换判定模型 = 换实验条件（随 experiment_snapshot() 登记）。

移植说明：prompt 与三层校验逐字移植自 spike runner（run_spike_api.py，v2 全量 96 条实测口径），
改动 = 换实验条件，须重跑 v2 回归集（D20 纪律）。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import logger

# ============ 结果来源（origin 枚举——调用方据此区分"模型判断"与"兜底"）============
ORIGIN_PREFILTER = "prefilter"   # 确定性前置命中（正则短路）
ORIGIN_MODEL = "model"           # 主判定模型采纳
ORIGIN_UPGRADE = "upgrade"       # 升级档模型采纳
ORIGIN_FAILSAFE = "fail_safe"    # 兜底：调用/校验失败、低置信不采信、功能未启用
ORIGIN_DISABLED = "disabled"     # GATE_ENABLED=False 全程旁路

client = AsyncOpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)


@dataclass(frozen=True)
class GateResult:
    """一次判定的结果（对外契约——调用方只依赖本结构与五函数签名）。

    decision   : 该判断点的取值（枚举见 _TASKS；归属为 task / archive）
    confidence : 置信度——前置=1.0（规则确定）；fail_safe/disabled=0.0（无信息）；
                 低置信兜底时如实记录模型原值
    reason     : 一句话理由（含兜底原因，供埋点与答辩审查）
    origin     : 结果来源（ORIGIN_* 常量）
    latency_ms : 判定总耗时（毫秒；含重试与升级档尝试；前置/旁路=0）
    """
    decision: str
    confidence: float
    reason: str
    origin: str
    latency_ms: float = 0.0


# ============ 判定模式（与 D19 定案一致：极简 prompt + 枚举写死 + 边界反例 + fail-safe 方向）============

_PAGE_FAULT_SYSTEM = """你是个人助手的记忆检索分诊判定器。只做一件事：判断"用户这句话"是否需要回顾历史记忆（回源检索）才能正确回应。

判定标准：
- yes（需要回源）：句子引用了过去发生的事或用户说过的信息（如"上次""之前""还记得""我提过"）；句子需要用户的个人信息才能回答（名字、住址、偏好、目标、人际关系、经历等）。
- no（不需要回源）：寒暄闲聊、通用知识问答、当场可完成的生成/翻译/计算任务。

判断示例：
- 输入"你还记得我上次说的搬家计划吗？" → yes（引用历史）
- 输入"今天天气怎么样？" → no（闲聊，无需记忆）

只输出一个 json 对象，不要输出任何其他文字。格式：
{"decision": "yes 或 no", "confidence": 0到1之间的小数, "reason": "一句话理由"}"""

_SUPERSEDE_SYSTEM = """你是记忆写入系统的去重裁决器。给你一张已存在的旧卡片和一条从新对话中抽出的新信息，判断两者的关系：
- unrelated：主题不同，互不影响
- coexist：新信息是补充或新增，两者可同时成立（如又养了第二只宠物、学会新技能）
- supersede：新信息改写了旧卡片的同一个属性，旧内容已过期（如搬家、换工作、改目标）

判断示例：
- 旧卡片"用户住在上海浦东" + 新信息"用户搬到了北京朝阳" → supersede（同一属性被改写）
- 旧卡片"用户会说英语" + 新信息"用户还会说日语" → coexist（能力叠加）
- 旧卡片"用户住在上海" + 新信息"用户喜欢吃辣" → unrelated（主题不同）

只输出一个 json 对象，不要输出任何其他文字。格式：
{"decision": "unrelated 或 coexist 或 supersede", "confidence": 0到1之间的小数, "reason": "一句话理由"}"""

_BACKFILL_SYSTEM = """你是记忆系统的因果回填判定器。给你两条事件记忆 A 和 B，判断两者之间是否存在明确、可解释的因果关系（B 是 A 的原因，或 A 是 B 的原因）。
- yes：存在明确因果，能用一句话解释"因为 B 所以 A"或"因为 A 所以 B"
- no：仅仅时间相邻、主题无关、或无法给出合理解释的关联（错误的因果链比缺失更危险）

判断示例：
- A"用户开始每天早上跑步" + B"用户体检发现血脂偏高，医生建议多运动" → yes（健康问题促使开始运动）
- A"用户买了一台新电脑" + B"用户周末去了动物园" → no（时间相邻但无因果解释）

只输出一个 json 对象，不要输出任何其他文字。格式：
{"decision": "yes 或 no", "confidence": 0到1之间的小数, "reason": "一句话理由"}"""

_SHORTLIST_SYSTEM = """你是记忆注入前的精筛判定器。给你用户当前的问题和一张候选记忆卡片，判断这张卡片对回答问题的价值档位：
- low：不相关，对回答没有帮助（包括已过期的旧状态）
- mid：部分相关，有参考价值但不是回答所必需
- high：高度相关，直接构成或决定回答内容

判断示例：
- 问题"我周末去哪露营好？" + 卡片"用户两个月前买过一顶帐篷" → mid（有关联但非必需）
- 问题"我对什么坚果过敏？" + 卡片"用户对花生严重过敏" → high（直接决定回答）
- 问题"推荐一部电影" + 卡片"用户住在上海浦东新区" → low（与问题无关）

只输出一个 json 对象，不要输出任何其他文字。格式：
{"decision": "low 或 mid 或 high", "confidence": 0到1之间的小数, "reason": "一句话理由"}"""

_TASKS: dict[str, dict] = {
    "page_fault": {
        "label": "缺页判定（是否需要回源）",
        "system": _PAGE_FAULT_SYSTEM,
        "user_template": "用户消息：{query}",
        "options": ("yes", "no"),
        "failsafe": "yes",
    },
    "supersede": {
        "label": "取代判定（无关/并存/取代）",
        "system": _SUPERSEDE_SYSTEM,
        "user_template": "旧卡片：{old_card}\n新信息：{new_info}",
        "options": ("unrelated", "coexist", "supersede"),
        "failsafe": "coexist",
    },
    "backfill": {
        "label": "回填判定（是否存在因果）",
        "system": _BACKFILL_SYSTEM,
        "user_template": "事件 A：{event_a}\n事件 B：{event_b}",
        "options": ("yes", "no"),
        "failsafe": "no",
    },
    "shortlist": {
        "label": "精筛判定（卡片相关档位）",
        "system": _SHORTLIST_SYSTEM,
        "user_template": "用户当前问题：{query}\n候选记忆卡片：{card}",
        "options": ("low", "mid", "high"),
        "failsafe": "low",
    },
}

_OWNERSHIP_FAILSAFE = "archive"  # 归属判定 fail-safe：归档案（任务卡后置，V1 不启用）


# ============ 三层校验（格式 → schema → 语义；与 spike 定案口径逐字一致）============

def _strip_fence(text: str) -> str:
    """剥离 markdown 代码围栏（模型偶发包装）。"""
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t[3:]
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3].rstrip()
    return t


def _three_layer_check(task_key: str, raw: str) -> tuple[dict | None, str | None]:
    """三层校验：通过 → (parsed, None)；任一层不过 → (None, 失败原因)。"""
    try:
        obj = json.loads(_strip_fence(raw))
    except Exception as e:  # noqa: BLE001
        return None, f"format: JSON 解析失败({e})"
    if not isinstance(obj, dict):
        return None, "format: 顶层不是 json 对象"
    missing = [k for k in ("decision", "confidence", "reason") if k not in obj]
    if missing:
        return None, f"schema: 缺字段 {missing}"
    decision = obj["decision"]
    if not isinstance(decision, str) or decision.strip().lower() not in _TASKS[task_key]["options"]:
        return None, f"schema: decision 非法 {decision!r}"
    try:
        conf = float(obj["confidence"])
    except (TypeError, ValueError):
        return None, f"schema: confidence 非法 {obj['confidence']!r}"
    if not (0.0 <= conf <= 1.0):
        return None, f"semantic: confidence 越界 {conf}"
    if not isinstance(obj["reason"], str) or not obj["reason"].strip():
        return None, "semantic: reason 为空"
    return {"decision": decision.strip().lower(), "confidence": round(conf, 4),
            "reason": obj["reason"].strip()}, None


# ============ 确定性前置（"少而准"：共 4 条高置信模式；仅缺页判定）============
# 设计原则：宁漏勿误——命中即短路（省一次模型调用），不命中一律交模型。
# 钩子模式优先级高于寒暄模式：含历史钩子的消息即使形似寒暄也走 yes（宁多勿漏）。

_HOOK_RE = re.compile(
    r"还记得"
    r"|我(?:之前|上次|以前|上回)(?:说|讲|提|问|定|买|约|告诉|聊)"
    r"|(?:我们|咱们)(?:之前|上次|说好|约定|商量)"
)

_SMALLTALK_RE = re.compile(
    r"\s*(?:你好|您好|hi|hello|哈喽|哈啰|在吗|早上好|下午好|晚上好|晚安|谢谢|谢啦|"
    r"好的|好呀|好嘞|行|嗯+|哈哈+|嘿)[啊呀呢啦嘛！!。.？?~～\s]*",
    re.IGNORECASE,
)


def _prefilter_page_fault(query: str) -> GateResult | None:
    """确定性前置：高置信模式短路（仅缺页）。返回 None = 未命中，交模型判定。"""
    text = query.strip()
    if _HOOK_RE.search(text):
        return GateResult("yes", 1.0, "确定性前置：显式历史钩子（宁多勿漏）",
                          ORIGIN_PREFILTER)
    if _SMALLTALK_RE.fullmatch(text):
        return GateResult("no", 1.0, "确定性前置：纯寒暄/致意（无需记忆）",
                          ORIGIN_PREFILTER)
    return None


# ============ 模型调用与分档主循环 ============

def _primary_model() -> str:
    """主判定模型：GATE_MODEL 未配置时跟随 LLM_MODEL。"""
    return settings.GATE_MODEL or settings.LLM_MODEL


async def _call_model(model: str, task_key: str, user_content: str,
                      json_mode: bool = True) -> str:
    """单次约束模式调用，返回原始文本；异常向上抛（由分档循环接管）。"""
    spec = _TASKS[task_key]
    messages = [
        {"role": "system", "content": spec["system"]},
        {"role": "user", "content": user_content},
    ]
    kwargs: dict = dict(model=model, messages=messages, temperature=0.0, top_p=0.1,
                        timeout=settings.GATE_TIMEOUT_SECONDS)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def _run_judgement(task_key: str, inputs: dict,
                         prefilter: Callable[[], GateResult | None] | None = None) -> GateResult:
    """分档判定主循环（内部唯一实现，五函数共用）：

    旁路检查 → 确定性前置 → 主档（约束判定，失败重试 1 次=纯 prompt 模式）
    → 升级档（可选，一次）→ fail-safe。任何路径都不抛异常。
    """
    spec = _TASKS[task_key]
    failsafe = spec["failsafe"]
    user_content = spec["user_template"].format(**inputs)

    # ① 全程旁路：GATE_ENABLED=False（消融档位）——不跑前置、不调模型
    if not settings.GATE_ENABLED:
        return GateResult(failsafe, 0.0,
                          "GATE_ENABLED=False：判定器旁路（保守方向）", ORIGIN_DISABLED)

    # ② 确定性前置：高置信短路（memories 原则："少而准"；仅缺页启用）
    if prefilter is not None and settings.GATE_PREFILTER_ENABLED:
        hit = prefilter()
        if hit is not None:
            logger.debug(f"[gate] 确定性前置命中 task={task_key} → {hit.decision}")
            return hit

    t0 = time.perf_counter()
    elapsed = lambda: round((time.perf_counter() - t0) * 1000, 1)  # noqa: E731

    def fallback(reason: str, confidence: float = 0.0) -> GateResult:
        logger.warning(f"[gate] 兜底 task={task_key} → {failsafe}（{reason}）")
        return GateResult(failsafe, confidence, f"兜底（{reason}）", ORIGIN_FAILSAFE, elapsed())

    # ③ 主档：约束判定 ×2（第 1 次 json_object；重试=纯 prompt 模式，
    #    同时兜住偶发抖动与端点不支持 response_format 两种情况——spike §3 原设计口径）
    primary = _primary_model()
    last_error = "未知原因"
    for attempt, json_mode in ((1, True), (2, False)):
        try:
            raw = await _call_model(primary, task_key, user_content, json_mode=json_mode)
            parsed, fail_reason = _three_layer_check(task_key, raw)
        except Exception as exc:  # noqa: BLE001 —— 任何调用异常都走重试/兜底
            last_error = f"调用异常 {type(exc).__name__}: {exc}"
            logger.debug(f"[gate] task={task_key} 第 {attempt} 次调用异常：{last_error}")
            continue
        if parsed is None:
            last_error = fail_reason
            logger.debug(f"[gate] task={task_key} 第 {attempt} 次未过三层校验：{fail_reason}")
            continue
        # 低置信：不采信模型结论，按保守方向（D19："低置信按保守方向处理"）
        if parsed["confidence"] < settings.GATE_CONFIDENCE_MIN:
            return fallback(
                f"低置信({parsed['confidence']})不采信；模型原判 {parsed['decision']}：{parsed['reason']}",
                confidence=parsed["confidence"])
        return GateResult(parsed["decision"], parsed["confidence"], parsed["reason"],
                          ORIGIN_MODEL, elapsed())

    # ④ 升级档（可选，D19）：主档两次均无合法结果时，试一次更强模型
    if settings.GATE_UPGRADE_MODEL:
        try:
            raw = await _call_model(settings.GATE_UPGRADE_MODEL, task_key, user_content)
            parsed, fail_reason = _three_layer_check(task_key, raw)
            if parsed is not None and parsed["confidence"] >= settings.GATE_CONFIDENCE_MIN:
                return GateResult(parsed["decision"], parsed["confidence"],
                                  f"[升级档] {parsed['reason']}", ORIGIN_UPGRADE, elapsed())
            last_error = fail_reason or f"升级档低置信({parsed['confidence']})"
        except Exception as exc:  # noqa: BLE001
            last_error = f"升级档调用异常 {type(exc).__name__}: {exc}"

    # ⑤ fail-safe：走工程规范 §3 默认方向表
    return fallback(last_error)


# ============ 五个对外判定函数（单一出口的接口契约）============

async def judge_page_fault(query: str) -> GateResult:
    """缺页判定：这句话要不要回源检索记忆？（yes=回源；fail-safe=yes，宁多勿漏）

    调用方：retriever（E9）。置信门槛与前置短路见模块头。
    """
    return await _run_judgement("page_fault", {"query": query},
                                prefilter=lambda: _prefilter_page_fault(query))


async def judge_supersede(old_card: str, new_info: str) -> GateResult:
    """取代判定：新信息与旧卡片的关系？（unrelated / coexist / supersede；fail-safe=coexist）

    调用方：writer（E8）。误取代丢旧卡不可逆，宁并存不误取代。
    """
    return await _run_judgement("supersede", {"old_card": old_card, "new_info": new_info})


async def judge_backfill(event_a: str, event_b: str) -> GateResult:
    """回填判定：两条事件记忆之间是否存在明确因果？（yes=连线；fail-safe=no，不连线）

    调用方：writer（E8）。错误因果链比缺失更危险。
    """
    return await _run_judgement("backfill", {"event_a": event_a, "event_b": event_b})


async def judge_shortlist(query: str, card: str) -> GateResult:
    """精筛判定：候选卡片对当前问题的价值档位？（low / mid / high；fail-safe=low，不注入）

    调用方：reranker（E10）。硬阈值门（GATE_THRESHOLD）由调用方据此估算分值执行。
    """
    return await _run_judgement("shortlist", {"query": query, "card": card})


async def judge_ownership(text: str) -> GateResult:
    """任务/档案归属：这条信息属于任务卡还是档案？（task / archive；fail-safe=archive）

    V1 不启用（任务卡后置，系统设计 §7）——恒返回 fail-safe 方向（归档案），
    签名保留为接口契约（五判断点单一出口），实现随任务卡机制一并后置。
    """
    _ = text  # 参数保留为接口契约（E12+ 任务卡实现时启用）
    return GateResult(_OWNERSHIP_FAILSAFE, 0.0,
                      "归属判定 V1 未启用（任务卡后置，系统设计 §7）；按 fail-safe 归档案",
                      ORIGIN_FAILSAFE)
