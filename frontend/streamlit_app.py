import streamlit as st
import requests
import uuid
import json

API_BASE = "http://127.0.0.1:8000"
CHAT_URL = f"{API_BASE}/api/v1/chat/"
AUTH_URL = f"{API_BASE}/api/v1/auth"
SESSION_URL = f"{API_BASE}/api/v1/sessions"

st.set_page_config(page_title="智路由 AI 客服", page_icon="🤖", layout="wide")

if "token" not in st.session_state:
    st.session_state.token = None
if "user" not in st.session_state:
    st.session_state.user = None
if "session_id" not in st.session_state:
    st.session_state.session_id = f"user_{uuid.uuid4().hex[:8]}"
if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "assistant", "content": "您好！我是智路由 AI 客服，请问有什么可以帮您？"}]


def _headers():
    h = {}
    if st.session_state.token:
        h["Authorization"] = f"Bearer {st.session_state.token}"
    return h


def _refresh_sessions():
    if not st.session_state.token:
        return []
    try:
        resp = requests.get(SESSION_URL + "/", headers=_headers(), timeout=10)
        if resp.ok:
            return resp.json()
    except Exception:
        pass
    return []


if not st.session_state.token:
    st.title("🤖 智路由 AI 客服系统")
    tab1, tab2, tab3 = st.tabs(["登录", "注册", "匿名体验"])

    with tab1:
        with st.form("login_form"):
            u = st.text_input("用户名")
            p = st.text_input("密码", type="password")
            if st.form_submit_button("登录", use_container_width=True):
                try:
                    resp = requests.post(f"{AUTH_URL}/login", json={"username": u, "password": p}, timeout=10)
                    if resp.ok:
                        data = resp.json()
                        st.session_state.token = data["access_token"]
                        st.session_state.user = data["user"]
                        st.session_state.messages = [{"role": "assistant", "content": f"欢迎回来，{data['user']['username']}！"}]
                        st.rerun()
                    else:
                        st.error(resp.json().get("detail", "登录失败"))
                except Exception as e:
                    st.error(f"连接失败：{e}")

    with tab2:
        with st.form("register_form"):
            u = st.text_input("用户名")
            p = st.text_input("密码", type="password")
            p2 = st.text_input("确认密码", type="password")
            if st.form_submit_button("注册", use_container_width=True):
                if p != p2:
                    st.error("两次密码不一致")
                elif len(p) < 6:
                    st.error("密码至少6位")
                else:
                    try:
                        resp = requests.post(f"{AUTH_URL}/register", json={"username": u, "password": p}, timeout=10)
                        if resp.ok:
                            data = resp.json()
                            st.session_state.token = data["access_token"]
                            st.session_state.user = data["user"]
                            st.session_state.messages = [{"role": "assistant", "content": f"注册成功！欢迎，{data['user']['username']}！"}]
                            st.rerun()
                        else:
                            st.error(resp.json().get("detail", "注册失败"))
                    except Exception as e:
                        st.error(f"连接失败：{e}")

    with tab3:
        st.info("匿名模式下，会话仅保存在本地，刷新页面后历史将丢失。")
        if st.button("继续匿名体验", use_container_width=True):
            st.session_state.messages = [{"role": "assistant", "content": "您好！我是智路由 AI 客服，请问有什么可以帮您？"}]
            st.rerun()

    st.stop()


with st.sidebar:
    st.subheader(f"👤 {st.session_state.user['username']}")

    if st.button("➕ 新建对话", use_container_width=True):
        try:
            resp = requests.post(SESSION_URL + "/", json={"title": "新对话"}, headers=_headers(), timeout=10)
            if resp.ok:
                data = resp.json()
                st.session_state.session_id = data["session_id"]
                st.session_state.messages = [{"role": "assistant", "content": "您好！我是智路由 AI 客服，请问有什么可以帮您？"}]
                st.rerun()
        except Exception:
            st.error("创建失败")

    st.divider()
    st.caption("会话列表")

    sessions = _refresh_sessions()
    for s in sessions:
        col1, col2 = st.columns([4, 1])
        with col1:
            active = "📌" if s["session_id"] == st.session_state.session_id else ""
            if st.button(f"{s.get('title', '对话')} {active}", key=f"sid_{s['session_id']}", use_container_width=True):
                st.session_state.session_id = s["session_id"]
                st.session_state.messages = [{"role": "assistant", "content": "您好！我是智路由 AI 客服，请问有什么可以帮您？"}]
                st.rerun()
        with col2:
            if st.button("🗑️", key=f"del_{s['session_id']}"):
                requests.delete(f"{SESSION_URL}/{s['session_id']}", headers=_headers(), timeout=10)
                if s["session_id"] == st.session_state.session_id:
                    st.session_state.session_id = f"user_{uuid.uuid4().hex[:8]}"
                    st.session_state.messages = [{"role": "assistant", "content": "您好！我是智路由 AI 客服，请问有什么可以帮您？"}]
                st.rerun()

    st.divider()
    if st.button("🚪 退出登录", use_container_width=True):
        st.session_state.token = None
        st.session_state.user = None
        st.rerun()


st.title("🤖 智路由 AI 客服系统")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if user_input := st.chat_input("请输入您的问题..."):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    payload = {"session_id": st.session_state.session_id, "message": user_input}
    headers = _headers()

    with st.chat_message("assistant"):
        with st.spinner("AI 正在思考中..."):
            try:
                response = requests.post(CHAT_URL, json=payload, headers=headers, timeout=30, stream=True)
                response.raise_for_status()

                message_placeholder = st.empty()
                full_reply = ""
                status_placeholder = st.empty()

                for line in response.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8')
                        if decoded_line.startswith("data: "):
                            data_str = decoded_line[6:]
                            try:
                                data_json = json.loads(data_str)
                                event_type = data_json.get("event")
                                content = data_json.get("content", "")

                                if event_type == "status":
                                    status_placeholder.markdown(f"*{content}*")
                                elif event_type == "message":
                                    status_placeholder.empty()
                                    full_reply += content
                                    message_placeholder.markdown(full_reply + "▌")
                                elif event_type == "error":
                                    full_reply += f"\n\n**[系统故障]** {content}"
                                    message_placeholder.error(full_reply)
                                elif event_type == "done":
                                    message_placeholder.markdown(full_reply)
                            except json.JSONDecodeError:
                                pass

                st.session_state.messages.append({"role": "assistant", "content": full_reply})

            except requests.exceptions.RequestException as e:
                error_msg = f"与后端服务器通信失败：请检查 FastAPI 是否启动。\n错误详情: `{str(e)}`"
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg})