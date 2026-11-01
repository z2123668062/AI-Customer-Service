<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { store } from '../store'
import MemoryGraph from '../components/MemoryGraph.vue'

const nodes = ref([])
const links = ref([])
const cards = ref([])
const picked = ref(null)
const loading = ref(true)
const q = ref('')

const filteredPeople = computed(() => {
  const kw = q.value.trim().toLowerCase()
  const list = kw ? nodes.value.filter((n) => (n.name || '').toLowerCase().includes(kw)) : nodes.value
  return [...list].sort((a, b) => b.cards - a.cards)
})

function fmtDate(t) {
  return (t || '').slice(0, 10) || '无日期'
}

const byName = computed(() => {
  const m = new Map()
  for (const c of cards.value) {
    for (const e of c.entities || []) {
      const k = String(e).trim().toLowerCase()
      if (!m.has(k)) m.set(k, [])
      m.get(k).push(c)
    }
  }
  return m
})

const personCards = computed(() =>
  picked.value ? byName.value.get(picked.value.name.toLowerCase()) || [] : []
)
const factCards = computed(() => personCards.value.filter((c) => c.kind !== 'event').slice(0, 10))
// 没有 event_time 的卡不上时间轴：否则「入库那天」会被读成「事情发生那天」
const eventCards = computed(() =>
  personCards.value
    .filter((c) => c.kind === 'event' && c.event_time)
    .sort((a, b) => String(b.event_time).localeCompare(String(a.event_time)))
    .slice(0, 14)
)
const undatedEvents = computed(() =>
  personCards.value.filter((c) => c.kind === 'event' && !c.event_time)
)

const related = computed(() => {
  if (!picked.value) return []
  const id = picked.value.id
  return links.value
    .filter((l) => l.source === id || l.target === id)
    .map((l) => ({
      node: nodes.value.find((n) => n.id === (l.source === id ? l.target : l.source)),
      weight: l.weight,
    }))
    .filter((x) => x.node)
})

onMounted(async () => {
  try {
    const [g, list] = await Promise.all([api.graph(), api.cards({ limit: 500 })])
    nodes.value = g.nodes || []
    links.value = g.links || []
    cards.value = list.items || []
    picked.value = nodes.value.find((n) => n.cards > 0) || nodes.value[0] || null
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <main class="page">
    <header class="hero">
      <div class="kicker">MEMORY · {{ nodes.length }} 个人物 · {{ cards.length }} 张卡</div>
      <h2>它记得的，是一张网</h2>
      <p class="lede">
        每个点是一个人或地方，点越大记得越多；连线说明他们在同一段记忆里一起出现过。
        点一个点，看它记住了什么。
      </p>
    </header>

    <div v-if="loading" class="empty">正在展开…</div>

    <template v-else>
      <div class="graph-section">
        <aside class="people-list">
          <input v-model="q" class="psearch" placeholder="找人 / 找地方" />
          <div class="plist">
            <button
              v-for="n in filteredPeople"
              :key="n.id"
              class="prow"
              :class="{ on: picked && picked.id === n.id }"
              @click="picked = n"
            >
              <span class="pn">{{ n.name }}</span>
              <span class="pc">{{ n.cards }}</span>
            </button>
            <div v-if="!filteredPeople.length" class="empty">没找到</div>
          </div>
        </aside>
        <MemoryGraph :nodes="nodes" :links="links" :active="picked" @select="picked = $event" />
      </div>

      <section v-if="picked" class="detail">
        <div class="who">
          <h3>{{ picked.name }}</h3>
          <span class="who-meta">
            {{ picked.subtype }} · {{ picked.cards }} 张卡 · {{ picked.events }} 件事
          </span>
          <p class="who-desc">{{ picked.description }}</p>
        </div>

        <div class="detail-grid">
          <div>
            <div class="block-title">信息 · {{ personCards.length }}</div>
            <div v-for="c in factCards" :key="c.id" class="fact" @click="store.openCard(c.id)">
              {{ c.content }}
            </div>
            <div v-if="!factCards.length" class="empty">没有属性卡</div>
          </div>

          <div>
            <div class="block-title">事件 · 按时间</div>
            <div
              v-for="c in eventCards"
              :key="c.id"
              class="tl-item"
              @click="store.openCard(c.id)"
            >
              <div class="tl-date">{{ fmtDate(c.event_time) }}</div>
              <div class="tl-dot"></div>
              <div class="tl-text">{{ c.content }}</div>
            </div>
            <div v-if="!eventCards.length" class="empty">没有带日期的事件</div>

            <div v-if="undatedEvents.length" class="undated">
              <div class="block-title">无日期 · {{ undatedEvents.length }}</div>
              <div v-for="c in undatedEvents.slice(0, 5)" :key="c.id" class="fact" @click="store.openCard(c.id)">
                {{ c.content }}
              </div>
            </div>
          </div>
        </div>

        <div v-if="related.length" class="related">
          <div class="block-title">常一起出现</div>
          <div class="chips">
            <button class="chip" v-for="r in related" :key="r.node.id" @click="picked = r.node">
              {{ r.node.name }}
              <span class="k">×{{ r.weight }}</span>
            </button>
          </div>
        </div>
      </section>
    </template>
  </main>
</template>
