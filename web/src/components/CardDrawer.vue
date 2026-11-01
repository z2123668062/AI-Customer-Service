<script setup>
import { ref, watch, computed } from 'vue'
import { api } from '../api'
import { store } from '../store'

const card = ref(null)
const loading = ref(false)
const editing = ref(false)
const draft = ref('')
const saving = ref(false)
const err = ref('')

const STATUS = { active: '有效', superseded: '已被取代', revoked: '已撤销', evicted: '已下架' }

async function load(id) {
  loading.value = true
  err.value = ''
  editing.value = false
  try { card.value = await api.card(id) } catch (e) { err.value = String(e.message || e) } finally { loading.value = false }
}

watch(() => store.drawerId, (id) => { if (id) load(id) }, { immediate: true })

// 来源是批次级的：抽取按 8 条一批，卡片记的是整批。按文字重合度排序，前三条直接看。
const sources = computed(() => {
  const list = card.value?.sources || []
  const words = new Set((card.value?.content || '').toLowerCase().match(/[a-z\u4e00-\u9fa5]{2,}/g) || [])
  const score = (t) => {
    const ws = new Set((t || '').toLowerCase().match(/[a-z\u4e00-\u9fa5]{2,}/g) || [])
    let n = 0; for (const w of ws) if (words.has(w)) n++
    return n
  }
  return [...list].sort((a, b) => score(b.content) - score(a.content))
})
const topSources = computed(() => sources.value.slice(0, 3))
const restSources = computed(() => sources.value.slice(3))

function beginEdit() {
  draft.value = card.value.content
  editing.value = true
}
async function save() {
  saving.value = true
  try {
    const r = await api.updateCard(card.value.id, draft.value)
    await load(r.new_card_id || card.value.id)
    editing.value = false
    await store.refreshStats()
  } catch (e) { err.value = String(e.message || e) } finally { saving.value = false }
}
function fmt(t) { return (t || '').slice(0, 16).replace('T', ' ') }
</script>

<template>
  <aside class="drawer">
    <div class="drawer-head">
      <div>
        <div class="kicker">
          记忆卡 #{{ card?.id ?? '—' }}
          <span v-if="card" class="tag" :class="card.status">{{ STATUS[card.status] || card.status }}</span>
        </div>
        <div class="kicker" style="margin-top: 4px" v-if="card">
          {{ card.kind }}{{ card.subtype ? ' / ' + card.subtype : '' }}
          <template v-if="card.event_time"> · {{ fmt(card.event_time) }}</template>
        </div>
      </div>
      <button class="btn" @click="store.closeCard()">关闭</button>
    </div>

    <div v-if="loading" class="empty"><span class="spinner"><i></i><i></i><i></i></span></div>
    <div v-else-if="err" class="empty">{{ err }}</div>

    <template v-else-if="card">
      <textarea v-if="editing" v-model="draft" style="width: 100%; min-height: 120px" />
      <div v-else class="cardtext">{{ card.content }}</div>

      <div style="display: flex; gap: 8px; margin-bottom: 18px">
        <template v-if="editing">
          <button class="btn primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存为新版本' }}</button>
          <button class="btn" @click="editing = false">取消</button>
        </template>
        <template v-else>
          <button class="btn" @click="beginEdit">改一下</button>
          <button v-if="card.superseded_by" class="btn" @click="store.openCard(card.superseded_by)">
            已被 #{{ card.superseded_by }} 取代
          </button>
        </template>
      </div>

      <div class="block-title">来源原文 · {{ sources.length }} 条</div>
      <div v-if="!sources.length" class="empty" style="padding: 10px 0">这张卡没有记录来源（修复前写入的）</div>
      <template v-else>
        <div class="src" v-for="s in topSources" :key="s.id">
          <div class="h"><b>#{{ s.id }}</b><span>{{ s.role }}</span><span style="margin-left: auto">{{ fmt(s.created_at) }}</span></div>
          <div class="c">{{ s.content }}</div>
        </div>
        <details v-if="restSources.length" style="margin-top: 6px">
          <summary style="cursor: pointer; color: var(--ink-3); font-size: 12px">
            同批其余 {{ restSources.length }} 条
          </summary>
          <div class="src" v-for="s in restSources" :key="s.id" style="margin-top: 8px">
            <div class="h"><b>#{{ s.id }}</b><span>{{ s.role }}</span></div>
            <div class="c">{{ s.content }}</div>
          </div>
        </details>
      </template>

      <div class="block-title" style="margin-top: 20px">版本链</div>
      <div v-if="!card.versions?.length" class="empty" style="padding: 6px 0">没有旧版本</div>
      <div v-for="v in card.versions" :key="v.id" class="fact" @click="store.openCard(v.id)">
        #{{ v.id }} · {{ v.content.slice(0, 50) }}
      </div>
    </template>
  </aside>
</template>
