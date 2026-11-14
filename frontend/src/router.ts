import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from './api/client'
import WelcomeView from './views/WelcomeView.vue'
import LoginView from './views/LoginView.vue'
import DashboardView from './views/DashboardView.vue'
import LibraryView from './views/LibraryView.vue'
import AskView from './views/AskView.vue'
import MatchView from './views/MatchView.vue'
import PlansView from './views/PlansView.vue'
import RulesView from './views/RulesView.vue'
import AdminView from './views/AdminView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // meta.title 由 App.vue 顶栏读取，页面里不再各写一份标题
    { path: '/welcome', name: 'welcome', component: WelcomeView, meta: { public: true, title: '知校' } },
    /**
     * `/login` 继续保留，直接渲染同一个落地页并**自动开窗**。
     *
     * 不能删：旧书签、答辩脚本、CDP 验收都写着 `/login`；删掉之后它们会掉进
     * 未匹配路由，被守卫弹回落地页，看起来像"登录入口没了"。
     * `?open=1` 之外也认 `redirect`，这样未登录访问深链时落地页知道登完去哪。
     */
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: { public: true, title: '登录' },
      props: { autoOpen: true },
    },
    { path: '/', name: 'dashboard', component: DashboardView, meta: { title: '工作台' } },
    { path: '/library', name: 'library', component: LibraryView, meta: { title: '政策知识库' } },
    { path: '/ask', name: 'ask', component: AskView, meta: { title: '智能问答' } },
    { path: '/match', name: 'match', component: MatchView, meta: { title: '资格匹配' } },
    { path: '/plans', name: 'plans', component: PlansView, meta: { title: '计划与任务' } },
    // 数据中心是 R9 新增的管理员专属页：后端两条接口都挂了 @admin_required
    { path: '/admin', name: 'admin', component: AdminView, meta: { adminOnly: true, title: '数据中心' } },
    { path: '/rules', name: 'rules', component: RulesView, meta: { adminOnly: true, title: '规则管理' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  if (!getToken()) return { name: 'welcome', query: { redirect: to.fullPath } }
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
