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
    // meta.title 由 App.vue 顶栏读取，页面里不再各写一份标题
    { path: '/login', name: 'login', component: LoginView, meta: { public: true, title: '登录' } },
    { path: '/', name: 'dashboard', component: DashboardView, meta: { title: '工作台' } },
    { path: '/library', name: 'library', component: LibraryView, meta: { title: '政策知识库' } },
    { path: '/ask', name: 'ask', component: AskView, meta: { title: '智能问答' } },
    { path: '/match', name: 'match', component: MatchView, meta: { title: '资格匹配' } },
    { path: '/plans', name: 'plans', component: PlansView, meta: { title: '计划与任务' } },
    { path: '/rules', name: 'rules', component: RulesView, meta: { adminOnly: true, title: '规则管理' } },
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
