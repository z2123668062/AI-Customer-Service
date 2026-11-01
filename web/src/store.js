import { reactive } from 'vue'
import { api } from './api'

/** 全局状态：会话、记忆计数、抽屉。
 *  记忆是全局的（cards 不分会话），所以计数在切会话时保持不变——
 *  这个细节本身就在说明"隔离的是话题，不是记忆"。 */
export const store = reactive({
  sessions: [],
  sessionId: null,
  stats: { total: 0, by_status: {} },
  drawerId: null,
  drawerOpen: false,

  openCard(id) {
    this.drawerId = id
    this.drawerOpen = true
  },
  closeCard() {
    this.drawerOpen = false
  },

  async refreshSessions() {
    try {
      const r = await api.sessions()
      this.sessions = r.items || []
    } catch { this.sessions = [] }
  },

  async refreshStats() {
    try { this.stats = await api.stats() } catch { /* 后端没起也不阻塞 */ }
  },

  async bootstrap() {
    await Promise.all([this.refreshSessions(), this.refreshStats()])
    if (!this.sessionId && this.sessions.length) this.sessionId = this.sessions[0].session_id
  },
})

export function newSessionId() {
  return 's_' + Math.random().toString(16).slice(2, 10)
}
