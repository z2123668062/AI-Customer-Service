# frontend/views/memory_page.py
"""记忆面板页（E11）：浏览 / 筛选 / 编辑 / 淘汰日志 / 演示——前端只吃 HTTP。

三个标签页：
- 📋 记忆总览：统计指标 + 筛选 + 列表（行选中）→ 详情与操作
  （编辑修订 / 撤销 / 恢复 / 回滚 / 彻底删除 / 剪断边）
- 🧹 淘汰日志：sweep 记录（原因可解释——“记忆不越界”的证据面）
- 🎬 演示区：手动触发淘汰巡检 + 操作指引
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from frontend import api_client, theme  # noqa: E402
from frontend.api_client import ApiError  # noqa: E402

STATUS_OPTIONS = ["全部", "active", "superseded", "revoked", "evicted"]
KIND_OPTIONS = ["全部", "entity", "event"]
SUBTYPE_OPTIONS = ["全部", "identity", "preference", "goal", "relation"]

st.title("🗂️ 记忆面板")

try:
    stats = api_client.fetch_stats()
except ApiError as exc:
    st.error(f"无法连接后端：{exc}")
    st.stop()


# ============================================================
# 公共小部件
# ============================================================

def _fmt_time(value: str | None) -> str:
    return (value or "")[:19].replace("T", " ")


def _cards_dataframe(items: list[dict]) -> pd.DataFrame:
    rows = [{
        "id": c["id"],
        "状态": theme.status_label(c["status"]),
        "类型": c["kind"] + (f"/{c['subtype']}" if c.get("subtype") else ""),
        "内容": (c["content"][:60] + "…") if len(c["content"]) > 60 else c["content"],
        "来源": c["source"],
        "访问": c.get("access_count", 0),
        "创建时间": _fmt_time(c.get("created_at")),
    } for c in items]
    return pd.DataFrame(rows)


# ============================================================
# 标签页
# ============================================================

tab_overview, tab_log, tab_demo = st.tabs(["📋 记忆总览", "🧹 淘汰日志", "🎬 演示区"])

# ------------------------------------------------------------
# 一、记忆总览
# ------------------------------------------------------------
with tab_overview:
    by_status = stats.get("by_status", {})
    pool_info = stats.get("pool", {})
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("记忆总数", stats.get("total", 0))
    m2.metric("🟢 有效", by_status.get("active", 0))
    m3.metric("🟡 被取代", by_status.get("superseded", 0))
    m4.metric("🔴 已撤销", by_status.get("revoked", 0))
    m5.metric("⚪ 已下架", by_status.get("evicted", 0))

    f1, f2, f3, f4 = st.columns([1.1, 1, 1.2, 1])
    sel_status = f1.selectbox("状态", STATUS_OPTIONS,
                              format_func=lambda s: s if s == "全部" else theme.status_label(s))
    sel_kind = f2.selectbox("类型", KIND_OPTIONS)
    sel_subtype = f3.selectbox("子类（仅 entity）", SUBTYPE_OPTIONS)
    if f4.button("🔄 刷新列表", use_container_width=True):
        st.rerun()

    try:
        listing = api_client.fetch_cards(
            status=None if sel_status == "全部" else sel_status,
            kind=None if sel_kind == "全部" else sel_kind,
            subtype=None if sel_subtype == "全部" else sel_subtype,
            limit=200,
        )
    except ApiError as exc:
        st.error(f"列表加载失败:{exc}")
        st.stop()

    items = listing.get("items", [])
    st.caption(f"共 {listing.get('count', 0)} 条（最多显示 200 条，最新在前）——点选一行查看详情")
    if not items:
        st.info("没有符合条件的记忆。去对话页聊几句，或调整筛选条件。")
    else:
        df = _cards_dataframe(items)
        event = st.dataframe(df, use_container_width=True, hide_index=True, height=280,
                            on_select="rerun", selection_mode="single-row")
        selected_rows = event.selection.rows

        if selected_rows:
            card_id = int(df.iloc[selected_rows[0]]["id"])
            try:
                detail = api_client.fetch_card(card_id)
            except ApiError as exc:
                st.error(f"详情加载失败：{exc}")
                st.stop()

            st.divider()
            left, right = st.columns([3, 2])

            with left:
                st.markdown(
                    f"#### 卡片 #{detail['id']} {theme.status_badge(detail['status'])}"
                    f"　`{detail['kind']}`"
                    + (f" `{detail['subtype']}`" if detail.get("subtype") else ""),
                    unsafe_allow_html=True,
                )
                st.markdown(f'<div class="mem-detail">{detail["content"]}</div>',
                            unsafe_allow_html=True)
                meta = [f"来源：{detail['source']}",
                        f"访问：{detail.get('access_count', 0)} 次",
                        f"创建：{_fmt_time(detail.get('created_at'))}"]
                if detail.get("event_time"):
                    meta.append(f"事件时间：{_fmt_time(detail['event_time'])}")
                if detail.get("source_session_id"):
                    meta.append(f"来源会话：{detail['source_session_id']}")
                st.caption(" · ".join(meta))
                if detail.get("superseded_by"):
                    st.caption(f"⏫ 当前被 **#{detail['superseded_by']}** 取代（可回滚本卡以推翻它）")

                if detail.get("versions"):
                    st.markdown("**版本历史（本卡取代过的旧版本）**")
                    for v in detail["versions"]:
                        st.caption(f"↳ #{v['id']} [{theme.status_label(v['status'])}] "
                                   f"{v['content'][:60]}")

                edges = detail.get("edges", [])
                if edges:
                    st.markdown("**相关边（线）**")
                    edge_df = pd.DataFrame([{
                        "边id": e["id"],
                        "类型": e["kind"],
                        "起点": f"#{e['from_card_id']}",
                        "终点": f"#{e['to_card_id']}",
                        "状态": "✂️ 已剪断" if e.get("cut_at") else "在链",
                    } for e in edges])
                    st.dataframe(edge_df, use_container_width=True, hide_index=True)
                    cuttable = {e["id"]: f"#{e['id']} {e['kind']}（{e['from_card_id']}→{e['to_card_id']}）"
                                for e in edges
                                if e["kind"] != "supersedes" and not e.get("cut_at")}
                    if cuttable:
                        with st.expander("✂️ 剪断一条因果边"):
                            st.caption("剪断后该边不再参与检索顺线（审计仍可见）；版本边请用回滚。")
                            edge_id = st.selectbox("选择边", list(cuttable.keys()),
                                                   format_func=lambda x: cuttable[x])
                            if st.button("确认剪断", key="cut_edge_btn"):
                                try:
                                    api_client.cut_edge(edge_id)
                                    st.toast("已剪断")
                                    st.rerun()
                                except ApiError as exc:
                                    st.error(str(exc))

            with right:
                st.markdown("**操作**")
                status = detail["status"]

                if status == "active":
                    with st.popover("✏️ 编辑内容（生成新版本）", use_container_width=True):
                        edited = st.text_area("修订后的内容", value=detail["content"],
                                              height=140, key=f"edit_{card_id}")
                        if st.button("保存修订", key=f"edit_save_{card_id}", type="primary"):
                            try:
                                res = api_client.update_card(card_id, edited)
                                st.toast(f"已生成新版本 #{res['new_card_id']}")
                                st.rerun()
                            except ApiError as exc:
                                st.error(str(exc))
                    if st.button("🗑️ 撤销（软删）", use_container_width=True, key=f"del_{card_id}"):
                        try:
                            api_client.delete_card(card_id)
                            st.toast("已撤销（可恢复）")
                            st.rerun()
                        except ApiError as exc:
                            st.error(str(exc))

                if status in ("revoked", "evicted"):
                    if st.button("♻️ 恢复为有效", use_container_width=True, key=f"re_{card_id}"):
                        try:
                            api_client.restore_card(card_id)
                            st.toast("已恢复为 active")
                            st.rerun()
                        except ApiError as exc:
                            st.error(str(exc))

                if status == "superseded":
                    if st.button("⏪ 回滚到此版本", use_container_width=True, key=f"rb_{card_id}"):
                        try:
                            res = api_client.rollback_card(card_id)
                            st.toast(res.get("detail", "已回滚"))
                            st.rerun()
                        except ApiError as exc:
                            st.error(str(exc))

                st.divider()
                confirm = st.checkbox("我确认要彻底删除（不可恢复）", key=f"purge_ok_{card_id}")
                if st.button("❌ 彻底删除（被遗忘权）", use_container_width=True,
                             disabled=not confirm, key=f"purge_{card_id}"):
                    try:
                        api_client.purge_card(card_id)
                        st.toast("已物理删除（原文保留在 messages）")
                        st.rerun()
                    except ApiError as exc:
                        st.error(str(exc))
        else:
            st.caption("👆 在上表点选一行，即可查看详情与编辑操作。")

# ------------------------------------------------------------
# 二、淘汰日志
# ------------------------------------------------------------
with tab_log:
    st.markdown("#### 🧹 淘汰日志（最新在前）")
    st.caption("每次下架都留痕：规则确定性可解释（零 LLM）——目标过期 / 偏好衰减。")
    try:
        log_res = api_client.fetch_evict_log(limit=100)
    except ApiError as exc:
        st.error(f"日志加载失败：{exc}")
        st.stop()
    log_items = log_res.get("items", [])
    if not log_items:
        st.info("暂无淘汰记录：启动巡检 / 手动触发 / 评测调用都会写入日志。")
    else:
        log_df = pd.DataFrame([{
            "时间": _fmt_time(it.get("ts")),
            "卡片": f"#{it.get('card_id')}",
            "类型": f"{it.get('kind', '')}/{it.get('subtype') or '—'}",
            "规则": it.get("rule", ""),
            "原因": it.get("reason", ""),
            "分数": it.get("score", ""),
            "内容快照": (it.get("content") or "")[:40],
        } for it in log_items])
        st.dataframe(log_df, use_container_width=True, hide_index=True, height=420)

# ------------------------------------------------------------
# 三、演示区
# ------------------------------------------------------------
with tab_demo:
    st.markdown("#### ▶️ 手动触发淘汰巡检")
    st.caption("扫描全部有效记忆 → 按确定性规则判定下架：目标卡超过宽限期、偏好卡衰减到阈值以下。")
    if st.button("运行淘汰巡检", type="primary"):
        try:
            report = api_client.run_evict()
        except ApiError as exc:
            st.error(str(exc))
        else:
            d1, d2, d3 = st.columns(3)
            d1.metric("扫描卡数", report.get("scanned", 0))
            d2.metric("本次下架", report.get("evicted", 0))
            stage = report.get("stage_ms", {})
            d3.metric("耗时（ms）", round(sum(stage.values()), 1))
            if report.get("details"):
                st.dataframe(pd.DataFrame(report["details"]),
                             use_container_width=True, hide_index=True)
            else:
                st.success("本次没有需要下架的卡（规则：目标过期 / 偏好衰减）。")

    st.divider()
    st.markdown("#### 🎬 操作演示指引")
    st.markdown(
        "- **修订（版本化）**：总览里选一张有效卡 → ✏️ 编辑 → 旧版自动标“已被取代”，"
        "详情里可见版本链；\n"
        "- **版本回滚**：选一张“已被取代”的卡 → ⏪ 回滚 → 取代它的后继版本全部撤销、版本边剪断；\n"
        "- **撤销与恢复**：🗑️ 撤销（软删）→ ♻️ 恢复；\n"
        "- **被遗忘权**：❌ 彻底删除 —— 物理删卡与相关边（原文 messages 保留，边界见文档）；\n"
        "- **剪断因果边**：详情里选择一条因果边剪断——它不再参与检索顺线，审计视图仍可见。"
    )
