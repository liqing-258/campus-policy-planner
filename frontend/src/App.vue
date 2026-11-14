<script setup lang="ts">
/**
 * 应用外壳：顶栏 + 左侧导航 + 内容区。
 *
 * 布局参照已部署的「智能教室设备管控系统」（120.27.211.217）：
 * 同一套顶栏高度、同一组深色面板令牌、同样的"当前范围 + 用户块 + 退出"右侧结构，
 * 目的是让两个课程项目看起来出自同一个产品线，而不是两套各自为政的作业。
 *
 * 这里**不出现任何建设进度信息**（里程碑、接口条数、测试项数、阶段说明）。
 * 那些属于仓库文档与答辩材料；产品界面上只该有用户用得着的东西。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, setUnauthorizedHandler, type Health } from './api/client'
import { useAuthStore } from './stores/auth'
import { useQAStore } from './stores/qa'
import BubbleFx from './components/BubbleFx.vue'

const auth = useAuthStore()
const qa = useQAStore()
const router = useRouter()
const route = useRoute()
const health = ref<Health | null>(null)
const healthError = ref('')

onMounted(async () => {
  // 3001（令牌失效）由 API 层统一回调过来。这里**必须走完整的登出**而不是只跳登录页：
  // 问答缓存落在 sessionStorage 里，不随令牌失效，只跳页的话下一个人登录后
  // 一进问答页就能看到上一个人的提问与解答。隐私问题的修法就是"登出即清缓存"。
  setUnauthorizedHandler(() => {
    auth.logout()
    router.push({ name: 'login' })
  })
  await auth.restore()
  try {
    health.value = await api.get<Health>('/health')
  } catch (err) {
    healthError.value = err instanceof Error ? err.message : '服务连接失败'
  }
})

/**
 * 退出登录必须**同时清掉问答缓存**。
 *
 * 会话缓存放在 sessionStorage 里，登录态一换它不会自动失效——下一个在这台机器上
 * 登录的人（比如机房里换人用）一进问答页就会看到上一个人的提问与解答。
 * 这是隐私问题，不是体验问题。
 */
function logout() {
  qa.clear()
  auth.logout()
  router.push({ name: 'login' })
}

const pageTitle = computed(() => (route.meta.title as string) || '')

/**
 * 降级横幅的第二行：缺什么变量 + 找过哪些 .env。
 *
 * 存在的意义是让"我明明配过密钥"这种疑问在界面上就能自证：如果列表里
 * 项目根的 .env 标着"不存在"，那就是 exe 被放在别的目录、路径没对上；
 * 如果标着"已读到"却仍降级，那才是变量名写错或值为空。
 * 只用后端给的 set/missing，**前端拿不到也不显示密钥值**。
 */
const degradedHint = computed(() => {
  const cfg = health.value?.llm_config
  if (!cfg) return ''
  const parts: string[] = []
  if (cfg.missing?.length) parts.push(`缺 ${cfg.missing.join('、')}`)
  const files = cfg.env_files ?? []
  if (files.length) {
    const seen = files.map((f) => `${f.path}${f.found ? '（已读到）' : '（不存在）'}`).join('；')
    parts.push(`已查找 ${seen}`)
  }
  return parts.join('；')
})

/** 登录页不套外壳：套了会出现"顶栏 + 整页登录卡"两层，品牌写两遍 */
const isPublic = computed(() => Boolean(route.meta.public))

/**
 * 导航按角色过滤。分组是为了让"办事"和"查资料 / 管配置"分开——
 * 学生每天只用到前两组，管理员才需要第三组。
 *
 * 隐藏入口不是权限控制：`/rules` 与所有写接口后端都有 @admin_required，
 * 手工输地址也会被接口层拒绝（接口清单 §7）。
 */
const navGroups = computed(() => {
  const groups = [
    {
      title: '事务办理',
      items: [
        { to: '/', label: '工作台', icon: '▤' },
        { to: '/ask', label: '智能问答', icon: '?' },
        { to: '/match', label: '资格匹配', icon: '◎' },
        { to: '/plans', label: '计划与任务', icon: '☑' },
      ],
    },
    {
      title: '政策资料',
      items: [{ to: '/library', label: '政策知识库', icon: '▣' }],
    },
  ]
  if (auth.isAdmin) {
    groups.push({
      title: '系统管理',
      items: [
        // 数据中心排在规则管理之前：它是管理员每天都在看的，规则管理是偶尔改一次
        { to: '/admin', label: '数据中心', icon: '◱' },
        { to: '/rules', label: '规则管理', icon: '⚙' },
      ],
    })
  }
  return groups
})

const roleLabel = computed(() => {
  const map: Record<string, string> = { admin: '管理员', student: '学生' }
  return map[auth.user?.role ?? ''] ?? auth.user?.role ?? '未登录'
})

const initials = computed(() => (auth.displayName || '?').slice(0, 1).toUpperCase())
</script>

<template>
  <!-- 气泡光标与点击破裂：挂在最外层，公共页与内页都有。
       组件内部自己判断 reduced-motion 与触屏，不启用时它连 canvas 都不创建 -->
  <BubbleFx />

  <!-- 公共路由（落地页 / 登录页）独立成页，不套应用外壳 -->
  <router-view v-if="isPublic" />

  <div v-else class="app">
    <!-- 降级提示条：常驻不可关闭。未配模型时只说清"会发生什么"，不假装是故障 -->
    <div v-if="health && health.llm_mode === 'degraded'" class="degraded">
      <strong>降级模式</strong>
      <span>未配置模型密钥：问答只返回检索到的政策原文，不做归纳总结。其余功能不受影响。</span>
      <!-- 光说"未配置"等于把定位工作丢给用户：配过 key 的人只会觉得系统有 bug。
           这里把"缺哪个变量、找过哪些 .env"直接摆出来，一眼能看出是不是路径问题。 -->
      <span v-if="degradedHint" class="degraded-hint">{{ degradedHint }}</span>
    </div>

    <header class="topbar">
      <div class="brand">
        <span class="logo">知</span>
        <div class="brand-text">
          <strong>知校</strong>
          <small>校园政策理解与事务规划</small>
        </div>
      </div>

      <span class="divider" />

      <div class="page-title">{{ pageTitle }}</div>

      <div class="spacer" />

      <div v-if="auth.isLoggedIn" class="scope-label" title="所有数据按当前账号隔离">
        数据范围 · 本人
      </div>

      <div v-if="auth.isLoggedIn" class="who">
        <span class="avatar">{{ initials }}</span>
        <div class="who-text">
          <strong>{{ auth.displayName }}</strong>
          <small>{{ roleLabel }}</small>
        </div>
      </div>

      <button v-if="auth.isLoggedIn" class="btn btn-sm btn-ghost" @click="logout">退出</button>
    </header>

    <div class="main">
      <aside v-if="auth.isLoggedIn && !healthError" class="side">
        <nav>
          <template v-for="group in navGroups" :key="group.title">
            <div class="nav-title">{{ group.title }}</div>
            <router-link
              v-for="item in group.items"
              :key="item.to"
              :to="item.to"
              class="nav-item"
              active-class="on"
              :exact-active-class="item.to === '/' ? 'on' : ''"
            >
              <span class="nav-icon">{{ item.icon }}</span>
              <span>{{ item.label }}</span>
            </router-link>
          </template>
        </nav>

        <div class="side-foot">
          <div v-if="health" class="side-meta">
            <span class="dot" :class="health.db === 'ok' ? 'ok' : 'bad'" />
            服务正常 · 政策 {{ health.document_count }} 份
          </div>
          <div v-else-if="healthError" class="side-meta">
            <span class="dot bad" />
            服务未连接
          </div>
        </div>
      </aside>

      <section class="content">
        <div v-if="healthError" class="card">
          <h3>无法连接服务</h3>
          <p class="muted small">{{ healthError }}</p>
          <p class="muted tiny mono">请确认后端已启动（默认 http://127.0.0.1:5000）</p>
        </div>
        <router-view v-else />
      </section>
    </div>
  </div>
</template>

<style scoped>
.app {
  min-height: 100%;
  display: flex;
  flex-direction: column;
}

.degraded {
  display: flex;
  gap: var(--space-2);
  padding: 7px var(--space-6);
  background: var(--c-warn-bg);
  border-bottom: 1px solid var(--c-warn-border);
  color: var(--c-unknown);
  font-size: 12.5px;
}

.degraded-hint {
  opacity: 0.78;
  font-size: 11.5px;
  user-select: text;
}

.topbar {
  height: 56px;
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 0 var(--space-4);
  background: var(--c-surface);
  border-bottom: 1px solid var(--c-border);
  position: sticky;
  top: 0;
  z-index: 30;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.logo {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  /* 品牌渐变而非纯 primary：白字压在 #2fc7b4 上只有 2.1:1，
     压到渐变深端就够 4.5:1 了 */
  background: var(--grad-brand);
  color: #fff;
  font-weight: 700;
  font-size: 15px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}

.brand-text strong {
  font-size: 14.5px;
}

.brand-text small {
  font-size: 11px;
  color: var(--c-text-2);
}

.divider {
  width: 1px;
  height: 20px;
  background: var(--c-border-strong);
}

.page-title {
  font-size: 13px;
  color: var(--c-text-2);
}

.spacer {
  flex: 1;
}

.scope-label {
  font-size: 11.5px;
  color: var(--c-text-2);
  padding: 4px 9px;
  border-radius: 7px;
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  white-space: nowrap;
}

.who {
  display: flex;
  align-items: center;
  gap: 8px;
}

.avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--c-primary-weak);
  border: 1px solid var(--c-primary-border);
  color: var(--c-primary);
  font-size: 12.5px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
}

.who-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}

.who-text strong {
  font-size: 12.5px;
  font-weight: 600;
}

.who-text small {
  font-size: 11px;
  color: var(--c-text-2);
}

.main {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-height: 0;
}

.side {
  width: 196px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) 10px;
  background: var(--c-surface);
  border-right: 1px solid var(--c-border);
  position: sticky;
  top: 56px;
  align-self: flex-start;
  height: calc(100vh - 56px);
}

nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nav-title {
  font-size: 10.5px;
  letter-spacing: 0.6px;
  color: var(--c-dim);
  padding: 12px 10px 4px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 10px;
  border-radius: var(--radius-control);
  color: var(--c-text);
  font-size: 13.5px;
  border: 1px solid transparent;
}

.nav-item:hover {
  background: var(--c-surface-2);
}

.nav-item.on {
  /* 用 brand 渐变 + strong 底色：亮青碧铺底配白字只有 2.1:1，读不清。
     左侧那道 2px 亮线是"当前位置"的第二重信号，不依赖颜色深浅 */
  background: linear-gradient(90deg, var(--c-primary-strong), rgba(11, 127, 116, 0.55));
  border-color: var(--c-primary-strong);
  color: #fff;
  font-weight: 500;
  box-shadow: inset 2px 0 0 var(--c-primary);
}

.nav-icon {
  width: 16px;
  text-align: center;
  opacity: 0.85;
  font-size: 12.5px;
}

.side-foot {
  margin-top: auto;
  padding: var(--space-2) 10px;
  border-top: 1px solid var(--c-border);
}

.side-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--c-text-2);
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--c-dim);
  flex: none;
}

.dot.ok {
  background: var(--c-met);
}

.dot.bad {
  background: var(--c-not-met);
}

.content {
  flex: 1;
  min-width: 0;
  padding: var(--space-4) var(--space-6) var(--space-8);
  overflow-x: hidden;
}

@media (max-width: 900px) {
  .main {
    flex-direction: column;
  }
  .side {
    width: 100%;
    height: auto;
    position: static;
    border-right: none;
    border-bottom: 1px solid var(--c-border);
    flex-direction: row;
    flex-wrap: wrap;
    padding: var(--space-2);
  }
  nav {
    flex-direction: row;
    flex-wrap: wrap;
    gap: 4px;
  }
  .nav-title,
  .side-foot {
    display: none;
  }
  .brand-text small,
  .scope-label,
  .page-title {
    display: none;
  }
  .content {
    padding: var(--space-3);
  }
}
</style>
