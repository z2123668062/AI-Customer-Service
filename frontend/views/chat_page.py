# frontend/views/chat_page.py
"""对话页（E11 轻改；E12 接"本轮记忆"条）：气泡主题 / 空态引导 / 侧栏会话控制。

会话状态约定（跨页面共享）：
- session_id：本地生成（s_xxxxxxxx），"新建对话"仅更换它——长期记忆跨会话存续；
- messages：本前端回显缓存（真相在后端 messages 表）。

E12：消费 SSE `meta` 事件（后端下发"本轮记忆"三态：off / none / injected）——
在助手气泡顶部渲染主题色小条，答辩演示可见"这轮用了哪几张卡"。
"""
import sys
import uuid
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from frontend import api_client, theme  # noqa: E402
from frontend.api_client import ApiError  # noqa: E402

st.title("💬 对话")

if "session_id" not in st.session_state:
    st.session_state.session_id = f"s_{uuid.uuid4().hex[:8]}"
if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    if st.button("➕ 新建对话", use_container_width=True):
        st.session_state.session_id = f"s_{uuid.uuid4().hex[:8]}"
        st.session_state.messages = []
        st.rerun()
    st.caption(f"当前会话：`{st.session_state.session_id}`")

# —— 消息区（空态 → 欢迎卡）——
if not st.session_state.messages:
    st.markdown(
        '<div class="mem-welcome">'
        "<h3>🧠 你好，我是你的个人助手</h3>"
        "<p>我会在聊天中悄悄记住你的事情，并在以后需要的时候想起来。</p>"
        "<p>💡 试试告诉我：<b>“我搬到杭州了”</b> · <b>“我更喜欢喝美式”</b> · "
        "<b>“我下周要去上海出差”</b></p>"
        "</div>",
        unsafe_allow_html=True,
    )
else:
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# —— 输入区（SSE 流式）——
if user_input := st.chat_input("说点什么…（比如：记住我住在杭州）"):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        meta_placeholder = st.empty()      # 顶部："本轮记忆"条（meta 事件到达时填充）
        placeholder = st.empty()
        status_placeholder = st.empty()
        full_reply = ""
        try:
            for event, content in api_client.chat_stream(
                    st.session_state.session_id, user_input):
                if event == "status":
                    status_placeholder.markdown(f"*{content}*")
                elif event == "meta":
                    status_placeholder.empty()
                    if isinstance(content, dict):
                        meta_placeholder.markdown(
                            theme.memory_trace_line(content), unsafe_allow_html=True)
                elif event == "message":
                    status_placeholder.empty()
                    full_reply += content
                    placeholder.markdown(full_reply + "▌")
                elif event == "done":
                    placeholder.markdown(full_reply)
        except ApiError as exc:
            status_placeholder.empty()
            placeholder.empty()
            st.error(f"😵 {exc}")
            if not full_reply:
                full_reply = "（本轮请求失败，请检查后端服务后重试）"
                st.markdown(full_reply)
        st.session_state.messages.append({"role": "assistant", "content": full_reply})
