# frontend/api_client.py
"""后端 API 客户端（E11）：前端唯一 HTTP 出口——超时/错误统一处理，页面只消费结果。

约定：
- 基址默认 http://127.0.0.1:8000，可用环境变量 MEMORY_API_BASE 覆盖；
- 失败统一抛 ApiError（message 可直接展示给用户；后端错误体里的 reply 字段优先）；
- chat_stream 是生成器：yield (event, content)——SSE 事件原样转发；
- 读接口 fetch_*，写动作 update/delete/restore/rollback/purge/cut/run。
"""
import json
import os

import requests

API_BASE = os.environ.get("MEMORY_API_BASE", "http://127.0.0.1:8000")
CHAT_URL = f"{API_BASE}/api/v1/chat/"
MEMORY_URL = f"{API_BASE}/api/v1/memory"


class ApiError(Exception):
    """API 调用失败（网络 / 4xx / 5xx 统一形态）。"""


def _to_api_error(resp: requests.Response) -> ApiError:
    try:
        payload = resp.json()
        detail = payload.get("reply") or payload.get("detail") or resp.text
    except Exception:  # noqa: BLE001 —— 非 JSON 错误体
        detail = resp.text
    return ApiError(f"HTTP {resp.status_code}：{detail}")


def _request(method: str, url: str, *, timeout: float = 15.0, **kwargs) -> dict:
    try:
        resp = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.exceptions.RequestException as exc:
        raise ApiError(f"与后端通信失败（请确认服务已启动）：{exc}") from exc
    if resp.status_code >= 400:
        raise _to_api_error(resp)
    return resp.json()


def backend_ok() -> bool:
    """后端存活探测（sidebar 状态徽章用；秒级超时）。"""
    try:
        return requests.get(f"{API_BASE}/health", timeout=2).status_code == 200
    except requests.exceptions.RequestException:
        return False


# ============================================================
# 对话（SSE 流式）
# ============================================================

def chat_stream(session_id: str, message: str, timeout: float = 60.0):
    """POST /api/v1/chat/ → 生成器，yield (event, content)。"""
    try:
        resp = requests.post(CHAT_URL, json={"session_id": session_id, "message": message},
                             timeout=timeout, stream=True)
    except requests.exceptions.RequestException as exc:
        raise ApiError(f"与后端通信失败（请确认服务已启动）：{exc}") from exc
    if resp.status_code >= 400:
        raise _to_api_error(resp)
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8")
            if not decoded.startswith("data: "):
                continue
            try:
                payload = json.loads(decoded[6:])
            except json.JSONDecodeError:
                continue
            yield payload.get("event"), payload.get("content", "")
    except requests.exceptions.RequestException as exc:
        raise ApiError(f"连接中断：{exc}") from exc


# ============================================================
# 记忆管理（GET 读 / POST·PATCH·DELETE 写）
# ============================================================

def fetch_stats() -> dict:
    """GET /memory/stats —— 总览统计。"""
    return _request("GET", f"{MEMORY_URL}/stats")


def fetch_cards(*, status: str | None = None, kind: str | None = None,
                subtype: str | None = None, order: str = "desc",
                after_id: int | None = None, limit: int = 200) -> dict:
    """GET /memory —— 列表（status/kind/subtype 过滤；desc 取最近）。"""
    params = {"order": order, "limit": limit}
    if status:
        params["status"] = status
    if kind:
        params["kind"] = kind
    if subtype:
        params["subtype"] = subtype
    if after_id is not None:
        params["after_id"] = after_id
    return _request("GET", MEMORY_URL, params=params)


def fetch_card(card_id: int) -> dict:
    """GET /memory/{id} —— 详情（双向边 + 版本链 + 取代者）。"""
    return _request("GET", f"{MEMORY_URL}/{card_id}")


def fetch_evict_log(limit: int = 50) -> dict:
    """GET /memory/evict-log —— 淘汰日志（最新在前）。"""
    return _request("GET", f"{MEMORY_URL}/evict-log", params={"limit": limit})


def run_evict() -> dict:
    """POST /memory/evict —— 手动触发淘汰巡检。"""
    return _request("POST", f"{MEMORY_URL}/evict", timeout=30.0)


def update_card(card_id: int, content: str) -> dict:
    """PATCH /memory/{id} —— 内容修订（新版本 + supersedes）。"""
    return _request("PATCH", f"{MEMORY_URL}/{card_id}", json={"content": content})


def delete_card(card_id: int) -> dict:
    """DELETE /memory/{id} —— 撤销（软删 revoke）。"""
    return _request("DELETE", f"{MEMORY_URL}/{card_id}")


def restore_card(card_id: int) -> dict:
    """POST /memory/{id}/restore —— 恢复（revoked/evicted → active）。"""
    return _request("POST", f"{MEMORY_URL}/{card_id}/restore")


def rollback_card(card_id: int) -> dict:
    """POST /memory/{id}/rollback —— 版本回滚（后继链撤销 + 版本边剪断）。"""
    return _request("POST", f"{MEMORY_URL}/{card_id}/rollback")


def purge_card(card_id: int) -> dict:
    """DELETE /memory/{id}/purge —— 被遗忘权（物理删卡 + 相关边）。"""
    return _request("DELETE", f"{MEMORY_URL}/{card_id}/purge")


def cut_edge(edge_id: int) -> dict:
    """DELETE /memory/edges/{id} —— 剪断因果边。"""
    return _request("DELETE", f"{MEMORY_URL}/edges/{edge_id}")
