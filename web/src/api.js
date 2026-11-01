const BASE = '/api/v1'

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    let msg = `${res.status}`
    try { const j = await res.json(); msg = j.detail || j.reply || msg } catch { /* 非 JSON 错误体 */ }
    throw new Error(msg)
  }
  return res.json()
}

export const api = {
  stats: () => req('/memory/stats'),
  cards: (params = {}) => req('/memory?' + new URLSearchParams(params)),
  card: (id) => req(`/memory/${id}`),
  graph: () => req('/memory/graph'),
  sessions: () => req('/sessions'),
  sessionMessages: (id) => req(`/sessions/${id}/messages`),
  updateCard: (id, content) =>
    req(`/memory/${id}`, { method: 'PATCH', body: JSON.stringify({ content }) }),
  deleteCard: (id) => req(`/memory/${id}`, { method: 'DELETE' }),
  restoreCard: (id) => req(`/memory/${id}/restore`, { method: 'POST' }),
}

/** SSE 对话流：后端逐字推送，这里按 \n\n 切事件。
 *  useRerank 逐轮覆盖服务端默认：true = 逐卡精排（慢但准），false = 按分数排序（快）。 */
export async function streamChat(sessionId, message, useRerank, onEvent) {
  const res = await fetch(`${BASE}/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message, use_rerank: useRerank }),
  })
  if (!res.ok || !res.body) throw new Error(`对话失败：${res.status}`)
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let cut
    while ((cut = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, cut)
      buf = buf.slice(cut + 2)
      const line = chunk.split('\n').find((l) => l.startsWith('data:'))
      if (!line) continue
      try { onEvent(JSON.parse(line.slice(5).trim())) } catch { /* 忽略坏帧 */ }
    }
  }
}
