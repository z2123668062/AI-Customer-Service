# frontend/streamlit_app.py
import streamlit as st
import requests
import uuid

# 后端 FastAPI 服务的地址
API_URL = "http://127.0.0.1:8000/api/v1/chat/"

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
                # 1. 核心改变：开启 stream=True，告诉 requests 不要死等全部下载完，一来水就顺着管子流给我
                response = requests.post(API_URL, json=payload, timeout=30, stream=True)
                response.raise_for_status()

                # Streamlit 特有的流式空位占位符，用来把一滴滴滋出来的水打在同一个地方形成打字机效果
                message_placeholder = st.empty()
                full_reply = ""

                # 可选：搞一个专门显示“思考状态”的小占位符
                status_placeholder = st.empty()

                import json

                # 2. 从管子里遍历滴下来的每一行水流
                for line in response.iter_lines():
                    if line:
                        # 流式传输是 utf-8 byte格式，按规范通常是以 "data: " 打头的
                        decoded_line = line.decode('utf-8')
                        if decoded_line.startswith("data: "):
                            data_str = decoded_line[6:]  # 切掉前缀，拿出大括号

                            try:
                                data_json = json.loads(data_str)
                                event_type = data_json.get("event")
                                content = data_json.get("content", "")

                                # 根据我们在后端定好的 event 身份牌，做不同显示
                                if event_type == "status":
                                    # 显示状态（例如查天气中...），用暗色小字
                                    status_placeholder.markdown(f"*{content}*")
                                elif event_type == "message":
                                    # 正文！像打字机一样堆叠！然后瞬间刷新界面上的文字
                                    status_placeholder.empty()  # 把状态清空
                                    full_reply += content
                                    message_placeholder.markdown(full_reply + "▌")  # 加个闪烁小光标更真实
                                elif event_type == "error":
                                    full_reply += f"\n\n**[系统故障]** {content}"
                                    message_placeholder.error(full_reply)
                                elif event_type == "done":
                                    # 传输闭环结束，去掉小光标，定型
                                    message_placeholder.markdown(full_reply)

                            except json.JSONDecodeError:
                                # 有时候流式可能突然闪断半个括号，不用管继续等下一滴
                                pass

                # 传输彻底结束后，将最后完整的长篇大论保存进用户的历史记录状态，防止刷新丢失
                st.session_state.messages.append({"role": "assistant", "content": full_reply})

            except requests.exceptions.RequestException as e:
                # 捕获网络异常（比如 FastAPI 没启动）
                error_msg = f"与后端服务器通信失败：请检查 FastAPI 是否启动。\n错误详情: `{str(e)}`"
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg})