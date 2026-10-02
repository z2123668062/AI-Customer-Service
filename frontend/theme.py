# frontend/theme.py
"""前端主题（E11）：配色常量 + 全局 CSS 注入 + 小组件 HTML——纯视觉层，无业务逻辑。

配色与 .streamlit/config.toml 的 [theme] 对齐（primaryColor=#6C5CE7 紫）；
Streamlit 自带主题负责控件外观，本模块只管“控件之外”的组件样式
（聊天气泡 / 状态标签 / 侧栏统计卡 / 欢迎卡 / 详情卡）。
"""
import streamlit as st

PRIMARY = "#6C5CE7"

# 状态 → (中文标签, 颜色, emoji)——与后端 CARD_STATUSES 四值对齐（E11）
STATUS_META = {
    "active":     ("有效", "#00B894", "🟢"),
    "superseded": ("已被取代", "#F0A500", "🟡"),
    "revoked":    ("已撤销", "#E17055", "🔴"),
    "evicted":    ("已下架", "#8E8E9A", "⚪"),
}


def status_label(status: str) -> str:
    """列表用短标签（含 emoji，dataframe 里不能 HTML）。"""
    label, _, icon = STATUS_META.get(status, (status, "#8E8E9A", ""))
    return f"{icon} {label}"


def status_badge(status: str) -> str:
    """详情用 HTML 标签。"""
    label, color, _ = STATUS_META.get(status, (status, "#8E8E9A"))
    return f'<span class="mem-tag" style="background:{color}">{label}</span>'


def online_badge(online: bool) -> str:
    """后端在线状态徽章（sidebar）。"""
    dot = "#00B894" if online else "#E17055"
    text = "后端在线" if online else "后端离线"
    return f'<div class="mem-badge"><span class="dot" style="background:{dot}"></span>{text}</div>'


def memory_trace_line(meta: dict) -> str:
    """对话页"本轮记忆"条（E12：SSE meta 事件渲染）。

    三态：off（记忆读取关闭，基线档）/ none（本轮未召回）/ injected（注入 N 条 + 卡片摘要）。
    """
    status = meta.get("status")
    if status == "off":
        return '<div class="mem-trace">🧠 记忆读取已关闭（基线档）</div>'
    if status == "none":
        return '<div class="mem-trace">🧠 本轮未召回记忆</div>'
    cards = meta.get("cards") or []
    previews = " ｜ ".join(c.get("preview", "") for c in cards if c.get("preview"))
    count = meta.get("count", len(cards))
    return f'<div class="mem-trace">🧠 本轮记忆 {count} 条：{previews}</div>'


def stat_card(stats: dict) -> str:
    """侧栏记忆统计卡（数据来自 GET /api/v1/memory/stats）。"""
    by = stats.get("by_status", {})
    pool_info = stats.get("pool", {})
    rows = [
        ("记忆总数", stats.get("total", 0)),
        ("🟢 有效", by.get("active", 0)),
        ("🟡 被取代", by.get("superseded", 0)),
        ("🔴 已撤销", by.get("revoked", 0)),
        ("⚪ 已下架", by.get("evicted", 0)),
        ("⚡ 快表驻留", pool_info.get("cards", 0)),
    ]
    body = "".join(
        f'<div class="mem-stat-row"><span>{label}</span><b>{value}</b></div>'
        for label, value in rows
    )
    return f'<div class="mem-stat">{body}</div>'


def welcome_html() -> str:
    """对话页空态欢迎卡。"""
    return (
        '<div class="mem-welcome">'
        "<h3>🧠 你好，我是你的个人助手</h3>"
        "<p>我会在聊天中悄悄记住你的事情，并在以后需要的时候想起来。</p>"
        "<p>💡 试试告诉我：<b>“我搬到杭州了”</b> · <b>“我更喜欢喝美式”</b> · "
        "<b>“我下周要去上海出差”</b></p>"
        "</div>"
    )


CSS = """
<style>
/* —— 主区域留白微调 —— */
.block-container { padding-top: 2.0rem; }

/* —— 聊天气泡 —— */
[data-testid="stChatMessage"] {
    border-radius: 14px;
    padding: 0.65rem 1.0rem;
    margin-bottom: 0.45rem;
    border: 1px solid rgba(108, 92, 231, 0.12);
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}

/* —— 侧栏统计卡 —— */
.mem-stat {
    border: 1px solid rgba(108, 92, 231, 0.20);
    border-radius: 12px;
    padding: 0.55rem 0.8rem;
    background: rgba(108, 92, 231, 0.04);
}
.mem-stat .mem-stat-row {
    display: flex;
    justify-content: space-between;
    padding: 0.12rem 0;
    font-size: 0.85rem;
}

/* —— 在线徽章 —— */
.mem-badge { font-size: 0.8rem; color: #666; margin: 0.1rem 0 0.5rem; }
.mem-badge .dot {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; margin-right: 6px;
}

/* —— 状态标签 —— */
.mem-tag {
    display: inline-block; border-radius: 999px;
    padding: 0.08rem 0.55rem; font-size: 0.75rem; color: #fff;
}

/* —— 欢迎卡（对话空态）—— */
.mem-welcome {
    border: 1px dashed rgba(108, 92, 231, 0.35);
    background: rgba(108, 92, 231, 0.05);
    border-radius: 16px;
    padding: 1.2rem 1.5rem;
    margin-top: 0.8rem;
}
.mem-welcome h3 { margin: 0 0 0.5rem; }
.mem-welcome p { margin: 0.25rem 0; color: #444; }

/* —— 详情卡（记忆面板）—— */
.mem-detail {
    border: 1px solid rgba(108, 92, 231, 0.18);
    border-radius: 14px;
    padding: 0.9rem 1.1rem;
    background: rgba(108, 92, 231, 0.03);
}
.mem-detail .mem-meta { color: #666; font-size: 0.85rem; }

/* —— 本轮记忆条（对话页 meta 事件）—— */
.mem-trace {
    display: inline-block;
    font-size: 0.8rem;
    color: #6C5CE7;
    background: rgba(108, 92, 231, 0.08);
    border: 1px solid rgba(108, 92, 231, 0.18);
    border-radius: 999px;
    padding: 0.15rem 0.7rem;
    margin-bottom: 0.35rem;
}
</style>
"""


def apply() -> None:
    """注入全局 CSS（每个页面顶部调用一次）。"""
    st.markdown(CSS, unsafe_allow_html=True)
