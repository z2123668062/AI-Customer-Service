# frontend/streamlit_app.py
"""前端入口（E11 多页版）：对话页 + 记忆面板页，共用侧栏外壳。

启动（工作目录 = AI-Customer-Service）：
    streamlit run frontend/streamlit_app.py
后端需先启动：uvicorn app.main:app --reload

页面目录约定：views/（不用 pages/——那是 Streamlit 旧式 MPAv1 的保留目录名，
与 st.navigation 混用会触发自动发现警告）。
"""
import sys
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="记忆助手", page_icon="🧠", layout="wide",
                   initial_sidebar_state="expanded")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frontend import api_client, theme  # noqa: E402

theme.apply()

# —— 公共侧栏（所有页面共享：品牌 / 后端状态 / 记忆统计卡）——
with st.sidebar:
    st.markdown("### 🧠 记忆助手")
    online = api_client.backend_ok()
    st.markdown(theme.online_badge(online), unsafe_allow_html=True)
    if online:
        try:
            st.markdown(theme.stat_card(api_client.fetch_stats()), unsafe_allow_html=True)
        except api_client.ApiError:
            st.caption("统计暂不可用")
    else:
        st.caption("后端未启动：`uvicorn app.main:app --reload`")
    st.divider()

# —— 页面注册（st.navigation 多页）——
navigation = st.navigation([
    st.Page("views/chat_page.py", title="对话", icon="💬", default=True),
    st.Page("views/memory_page.py", title="记忆面板", icon="🗂️"),
])
navigation.run()
