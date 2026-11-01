<script setup>
import { ref, watch, onMounted, nextTick } from 'vue'
import { api, streamChat } from '../api'
import { store, newSessionId } from '../store'

const messages = ref([])
const input = ref('')
const busy = ref(false)
const thread = ref(null)
// 精排开关：默认关（服务端 .env 也是关）。选择记在本地，跨会话保留。
const useRerank = ref(localStorage.getItem('use_rerank') === '1')
watch(useRerank, (v) => localStorage.setItem('use_rerank', v ? '1' : '0'))

function scroll() {
  nextTick(() => {
    if (thread.value) thread.value.scrollTop = thread.value.scrollHeight
  })
}

async function load(id) {
  messages.value = []
  if (!id) { scroll(); return }
  try {
    const r = await api.sessionMessages(id)
    messages.value = (r.messages || []).map((m) => ({ role: m.role, content: m.content, cards: [] }))
  } catch { /* 新会话没有历史 */ }
  scroll()
}

watch(() => store.sessionId, (id) => load(id), { immediate: true })

async function send() {
  const text = input.value.trim()
  if (!text || busy.value) return
  if (!store.sessionId) store.sessionId = newSessionId()

  input.value = ''
  messages.value.push({ role: 'user', content: text, cards: [] })
  messages.value.push({ role: 'assistant', content: '', cards: [], streaming: true })
  // 取回响应式代理，直接改 push 进去的原始对象不会触发更新
  const asst = messages.value[messages.value.length - 1]
  busy.value = true
  scroll()

  try {
    await streamChat(store.sessionId, text, useRerank.value, (ev) => {
      if (ev.event === 'message') {
        asst.content += ev.content || ''
        scroll()
      } else if (ev.event === 'meta') {
        asst.cards = ev.content?.cards || []
        asst.raws = ev.content?.raws || []
      } else if (ev.event === 'done') {
        asst.streaming = false
      }
    })
  } catch (e) {
    asst.content += `（出错：${e.message}）`
  } finally {
    asst.streaming = false
    busy.value = false
    store.refreshSessions()
    store.refreshStats()
  }
}

/** 极简 markdown：只处理加粗与行内代码。
 *  模型爱输出 **xx**，直接当纯文本会露出星号，观感很差。先转义再替换，避免注入。 */
function md(t) {
  const esc = (t || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return esc
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

function onKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

onMounted(() => { if (!store.sessions.length) store.refreshSessions() })
</script>

<template>
  <main class="main">
    <div class="thread" ref="thread">
      <div v-if="!messages.length" class="void">
        <div class="void-orbit"><span></span><span></span><span></span></div>
        <h3>它已经记得 {{ store.stats.total || 0 }} 件事</h3>
        <p>说点什么，或者问它一句「我上次说要去哪」。<br />换个会话再问，它照样记得。</p>
      </div>

      <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
        <div class="avatar">{{ m.role === 'user' ? '你' : '鱼' }}</div>
        <div class="body">
          <div class="name">{{ m.role === 'user' ? '你' : '天才小羊' }}</div>
          <div class="bubble" v-html="md(m.content)"></div>
          <span v-if="m.streaming" class="caret"></span>
          <div v-if="(m.cards && m.cards.length) || (m.raws && m.raws.length)" class="chips">
            <button class="chip" v-for="c in m.cards" :key="c.id" @click="store.openCard(c.id)">
              <span class="dot"></span>
              {{ c.preview }}
              <span class="k">#{{ c.id }}</span>
            </button>
            <span v-if="m.raws && m.raws.length" class="chip raw" :title="m.raws.map(r => r.preview).join('\n')">
              原文 {{ m.raws.length }} 条
            </span>
          </div>
        </div>
      </div>
    </div>

    <div class="composer">
      <div class="composer-inner">
        <textarea
          v-model="input"
          rows="1"
          placeholder="随便说点什么…（回车发送，Shift+回车换行）"
          @keydown="onKey"
        ></textarea>
        <button class="btn primary" :disabled="busy || !input.trim()" @click="send">
          {{ busy ? '想…' : '发送' }}
        </button>
      </div>

      <div class="composer-tools">
        <button class="toggle" :class="{ on: useRerank }" @click="useRerank = !useRerank">
          <span class="track"><span class="knob"></span></span>
          <span>精排</span>
        </button>
        <span class="tool-hint">
          {{ useRerank ? '逐卡判定 · 慢但准（每轮约多花 8 秒）' : '按分数排序 · 快且省（约 2 秒）' }}
        </span>
      </div>
    </div>
  </main>
</template>
