<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, setUnauthorizedHandler, type Health } from './api/client'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const router = useRouter()
const health = ref<Health | null>(null)
const healthError = ref('')

onMounted(async () => {
  setUnauthorizedHandler(() => router.push({ name: 'login' }))
  await auth.restore()
  try {
    health.value = await api.get<Health>('/health')
  } catch (err) {
    healthError.value = err instanceof Error ? err.message : '健康检查失败'
  }
})

function logout() {
  auth.logout()
  router.push({ name: 'login' })
}

/**
 * 导航按角色过滤。规则管理只给管理员——但隐藏入口不是权限控制，
 * 后端 `/documents/{id}/rules/extract` 与 `PATCH /rules/{id}` 都有 admin_required。
 */
const navItems = computed(() => {
  const items = [
    { to: '/', label: '工作台', hint: '总览' },
    { to: '/library', label: '政策知识库', hint: 'M3' },
    { to: '/ask', label: '智能问答', hint: 'M4' },
    { to: '/match', label: '资格匹配', hint: 'M5' },
    { to: '/plans', label: '计划与任务', hint: 'M5' },
  ]
  if (auth.isAdmin) items.push({ to: '/rules', label: '规则管理', hint: 'M5' })
  return items
})
</script>

<template>
  <div class="shell">
    <!-- 降级提示条：常驻不可关闭（系统设计说明书 §8.3 第 6 条） -->
    <div v-if="health && health.llm_mode === 'degraded'" class="degraded">
      当前为<strong>降级模式</strong>：未配置模型密钥，问答将只返回检索原文，不做生成式总结。
      这不是故障，是设计上的诚实标注。
    </div>

    <header class="topbar">
      <div class="brand">
        <span class="dot" />
        <strong>知校</strong>
        <span class="muted tagline">校园政策理解与智能事务规划</span>
      </div>
      <nav v-if="auth.isLoggedIn" class="nav">
        <span class="muted">{{ auth.displayName }}（{{ auth.user?.role }}）</span>
        <button class="btn btn-ghost" @click="logout">退出</button>
      </nav>
    </header>

    <div class="body">
      <aside v-if="auth.isLoggedIn && !healthError" class="sidenav">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          active-class="active"
          :exact-active-class="item.to === '/' ? 'active' : ''"
        >
          <span>{{ item.label }}</span>
          <span class="nav-hint muted">{{ item.hint }}</span>
        </router-link>
      </aside>

      <main class="main">
        <div v-if="healthError" class="card error-card">
          <h3>后端未连接</h3>
          <p class="muted">{{ healthError }}</p>
          <p class="muted mono">请先运行：.venv\Scripts\python.exe server.py</p>
        </div>
        <router-view v-else />
      </main>
    </div>

    <footer class="footer muted">
      v{{ health?.version ?? '—' }} · 模型 {{ health?.llm_mode ?? '—' }} ·
      检索 {{ health?.embedding_mode ?? '—' }} ·
      政策文件 {{ health?.document_count ?? '—' }} 份 / 片段 {{ health?.chunk_count ?? '—' }} 条
    </footer>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

.degraded {
  padding: var(--space-3) var(--space-6);
  background: #fff8e6;
  border-bottom: 1px solid #f0dfae;
  color: #7a5b12;
  font-size: 13px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-6);
  background: var(--c-surface);
  border-bottom: 1px solid var(--c-border);
  position: sticky;
  top: 0;
  z-index: 10;
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--c-primary);
}

.tagline {
  font-size: 13px;
}

.nav {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.body {
  flex: 1;
  display: flex;
  align-items: flex-start;
  gap: var(--space-6);
  width: 100%;
  max-width: 1320px;
  margin: 0 auto;
  padding: var(--space-6);
}

.sidenav {
  width: 176px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  position: sticky;
  top: 76px;
}

.nav-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-control);
  color: var(--c-text);
  font-size: 14px;
  border: 1px solid transparent;
}

.nav-item:hover {
  background: var(--c-surface);
}

.nav-item.active {
  background: var(--c-primary-weak);
  border-color: #c9dcf5;
  color: var(--c-primary);
  font-weight: 500;
}

.nav-hint {
  font-size: 11px;
}

.main {
  flex: 1;
  min-width: 0;
}

.footer {
  padding: var(--space-4) var(--space-6);
  font-size: 12px;
  text-align: center;
  border-top: 1px solid var(--c-border);
}

.error-card {
  border-color: #f0dfae;
}

@media (max-width: 900px) {
  .body {
    flex-direction: column;
    padding: var(--space-4);
  }
  .sidenav {
    width: 100%;
    flex-direction: row;
    flex-wrap: wrap;
    position: static;
  }
  .nav-hint {
    display: none;
  }
  .tagline {
    display: none;
  }
}
</style>
