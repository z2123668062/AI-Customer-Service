# frontend/streamlit_app.py
import streamlit as st
import requests
import uuid

# 后端 FastAPI 服务的地址
API_URL = "http://127.0.0.1:8000/api/v1/chat"

st.set_page_config(page_title="智路由 AI 客服", page_icon="🤖", layout="wide")
st.title("🤖 智路由 AI 客服系统 (测试版)")

# ================================
# 1. 初始化会话状态 (Session State)
# ================================
# Streamlit 每次刷新都会重头运行代码，必须用 session_state 帮我们记住聊天记录和 session_id
if "session_id" not in st.session_state:
    st.session_state.session_id = f"user_{uuid.uuid4().hex[:8]}"

if "messages" not in st.session_state:
    # 放置一条默认的开场白
    st.session_state.messages = [{"role": "assistant", "content": "您好！我是智路由 AI 客服，请问有什么可以帮您？"}]

# ================================
# 2. 渲染历史聊天记录
# ================================
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ================================
# 3. 处理用户的新输入
# ================================
if user_input := st.chat_input("请输入您的问题...例如：帮我查一下尾号67890的物流"):

    # 立即把用户的话显示在界面上
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # 准备调用后端的请求体
    payload = {
        "session_id": st.session_state.session_id,
        "message": user_input
    }

    # 制造一个正在思考的 UI 效果
    with st.chat_message("assistant"):
        with st.spinner("AI 正在思考中..."):
            try:
                # 给 FastAPI 发送 POST 请求
                response = requests.post(API_URL, json=payload, timeout=30)
                response.raise_for_status()  # 如果返回了 4xx/5xx 则直接抛出异常跳到 except

                result = response.json()
                reply_text = result.get("reply", "服务器没有返回有效内容")

                # 【运营监控/调试视图】使用一个可折叠的 UI 来显示后台的链路数据
                trace_id = result.get("trace_id")
                with st.expander(f"⚙️ 调试信息 (Trace ID: {trace_id})", expanded=False):
                    st.json(result)  # 直接把后端原生的结构化数据打出来看

                # 渲染 AI 的回复
                st.markdown(reply_text)
                st.session_state.messages.append({"role": "assistant", "content": reply_text})

            except requests.exceptions.RequestException as e:
                # 捕获网络异常（比如你的 FastAPI 根本没启动）
                error_msg = f"与后端服务器通信失败：请检查 FastAPI 是否启动。\n错误详情: `{str(e)}`"
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg})