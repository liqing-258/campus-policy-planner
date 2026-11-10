import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from './api/client'
import LoginView from './views/LoginView.vue'
import DashboardView from './views/DashboardView.vue'
import LibraryView from './views/LibraryView.vue'
import AskView from './views/AskView.vue'
import MatchView from './views/MatchView.vue'
import PlansView from './views/PlansView.vue'
import RulesView from './views/RulesView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    { path: '/', name: 'dashboard', component: DashboardView },
    // M3：知识库
    { path: '/library', name: 'library', component: LibraryView },
    // M4：问答
    { path: '/ask', name: 'ask', component: AskView },
    // M5：匹配 / 计划 / 规则
    { path: '/match', name: 'match', component: MatchView },
    { path: '/plans', name: 'plans', component: PlansView },
    { path: '/rules', name: 'rules', component: RulesView, meta: { adminOnly: true } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  if (!getToken()) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.adminOnly) {
    // 角色判断只做体验层拦截，真正的权限由后端 @admin_required 决定。
    // 前端隐藏入口不等于安全——这点在接口清单 §7 里写清了。
    const raw = localStorage.getItem('zxx_user')
    try {
      const user = raw ? JSON.parse(raw) : null
      if (user && user.role !== 'admin') return { name: 'dashboard' }
    } catch {
      /* 解析失败就放行，交给后端 403 */
    }
  }
  return true
})

export default router
