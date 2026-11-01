<script setup>
import { onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { store, newSessionId } from './store'
import CardDrawer from './components/CardDrawer.vue'

const router = useRouter()
const route = useRoute()

onMounted(() => store.bootstrap())

function pick(id) {
  store.sessionId = id
  if (route.path !== '/') router.push('/')
}

function startNew() {
  store.sessionId = newSessionId()
  if (route.path !== '/') router.push('/')
}
</script>

<template>
  <div class="app">
    <aside class="side">
      <div class="brand">
        <h1><span class="fish"></span>小羊的记忆</h1>
        <div class="sub">记得 <b>{{ store.stats.total || 0 }}</b> 件事 · 跨会话共享</div>
      </div>

      <nav class="side-nav">
        <router-link to="/" :class="{ on: route.path === '/' }">对话</router-link>
        <router-link to="/memory" :class="{ on: route.path === '/memory' }">记忆</router-link>
      </nav>

      <div class="side-section">会话</div>
      <div class="session-list">
        <button
          v-for="s in store.sessions"
          :key="s.session_id"
          class="session-item"
          :class="{ on: s.session_id === store.sessionId }"
          @click="pick(s.session_id)"
        >
          <span class="t">{{ s.title }}</span>
          <span class="m">{{ s.messages }} 条</span>
        </button>
        <div v-if="!store.sessions.length" class="empty">还没有会话</div>
      </div>

      <div class="side-foot">
        <button class="btn wide primary" @click="startNew">新建对话</button>
      </div>
    </aside>

    <router-view />

    <CardDrawer v-if="store.drawerOpen" />
  </div>
</template>
