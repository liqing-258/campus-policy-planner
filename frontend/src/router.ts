import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from './api/client'
import LoginView from './views/LoginView.vue'
import DashboardView from './views/DashboardView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    { path: '/', name: 'dashboard', component: DashboardView },
    // M3 起逐个补齐：/library /ask /documents/:id /match /plan/:id /tasks /admin
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  if (getToken()) return true
  return { name: 'login', query: { redirect: to.fullPath } }
})

export default router
