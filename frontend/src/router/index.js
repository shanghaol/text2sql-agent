import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/LoginView.vue'), meta: { public: true } },
  { path: '/chat', name: 'chat', component: () => import('../views/ChatView.vue') },
  { path: '/settings', name: 'settings', component: () => import('../views/SettingsView.vue') },
  { path: '/history', name: 'history', component: () => import('../views/HistoryView.vue') },
  { path: '/', redirect: '/chat' },
  { path: '/:pathMatch(.*)*', redirect: '/chat' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (to.meta.public) {
    return auth.isLoggedIn ? '/chat' : true
  }
  if (!auth.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  // §4.3：进对话页（含其余受保护页）前本地 exp 预检，剩 <5min 先刷新，保证建流时 token 新鲜
  try {
    await auth.ensureFreshToken()
  } catch {
    return '/login'
  }
  return true
})

export default router
