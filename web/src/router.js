import { createRouter, createWebHistory } from 'vue-router'
import ChatView from './views/ChatView.vue'
import MemoryView from './views/MemoryView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: ChatView },
    { path: '/memory', name: 'memory', component: MemoryView },
  ],
})
