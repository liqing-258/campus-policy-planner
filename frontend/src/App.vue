<script setup lang="ts">
import { onMounted, ref } from 'vue'
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
        <span class="muted">校园政策理解与智能事务规划</span>
      </div>
      <nav v-if="auth.isLoggedIn" class="nav">
        <router-link to="/">工作台</router-link>
        <span class="muted">{{ auth.displayName }}（{{ auth.user?.role }}）</span>
        <button class="btn btn-ghost" @click="logout">退出</button>
      </nav>
    </header>

    <main class="main">
      <div v-if="healthError" class="card error-card">
        <h3>后端未连接</h3>
        <p class="muted">{{ healthError }}</p>
        <p class="muted mono">请先运行：.venv\Scripts\python.exe server.py</p>
      </div>
      <router-view v-else />
    </main>

    <footer class="footer muted">
      v{{ health?.version ?? '—' }} · 模型 {{ health?.llm_mode ?? '—' }} ·
      检索 {{ health?.embedding_mode ?? '—' }}
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

.nav {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.main {
  flex: 1;
  width: 100%;
  max-width: 1080px;
  margin: 0 auto;
  padding: var(--space-6);
}

.footer {
  padding: var(--space-4) var(--space-6);
  font-size: 12px;
  text-align: center;
}

.error-card {
  border-color: #f0dfae;
}

@media (max-width: 768px) {
  .main {
    padding: var(--space-4);
  }
  .brand .muted {
    display: none;
  }
}
</style>
