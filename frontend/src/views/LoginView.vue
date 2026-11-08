<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('student')
const password = ref('student123')

onMounted(async () => {
  if (await auth.restore()) router.replace({ name: 'dashboard' })
})

async function submit() {
  if (await auth.login(username.value.trim(), password.value)) {
    const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.replace(target)
  }
}

function fill(account: 'student' | 'admin') {
  username.value = account
  password.value = account === 'admin' ? 'admin123' : 'student123'
}
</script>

<template>
  <div class="login-wrap">
    <section class="card intro">
      <h1>知校</h1>
      <p class="muted">
        把校园政策变成你能执行的事项：<strong>找到依据 → 判断条件 → 生成计划 → 跟踪进度</strong>，
        每一步都能点回原文。
      </p>
      <ul class="points">
        <li>答案自带引用，点开即见原文与页码</li>
        <li>条件匹配分"符合 / 不符合 / 待补充"，不猜</li>
        <li>计划里的每个截止日期都能追到来源</li>
      </ul>
      <p class="muted small">
        当前进度：M2 工程骨架（登录 / 健康检查 / 建表）。政策库与问答按 M3、M4 逐步开放。
      </p>
    </section>

    <form class="card form" @submit.prevent="submit">
      <h3>登录</h3>
      <div class="field">
        <label for="u">用户名</label>
        <input id="u" v-model="username" autocomplete="username" />
      </div>
      <div class="field">
        <label for="p">密码</label>
        <input id="p" v-model="password" type="password" autocomplete="current-password" />
      </div>

      <p v-if="auth.error" class="err">{{ auth.error }}</p>

      <button class="btn" type="submit" :disabled="auth.loading">
        {{ auth.loading ? '登录中…' : '登录' }}
      </button>

      <div class="demo muted small">
        演示账号（虚构数据）：
        <button type="button" class="link" @click="fill('student')">student / student123</button>
        ·
        <button type="button" class="link" @click="fill('admin')">admin / admin123</button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: var(--space-6);
  align-items: start;
  margin-top: var(--space-8);
}

.intro h1 {
  margin-bottom: var(--space-3);
}

.points {
  margin: var(--space-4) 0;
  padding-left: 18px;
  color: var(--c-text-2);
}

.points li {
  margin-bottom: var(--space-1);
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.err {
  margin: 0;
  color: var(--c-not-met);
  font-size: 13px;
}

.demo {
  border-top: 1px dashed var(--c-border);
  padding-top: var(--space-3);
}

.small {
  font-size: 12px;
}

.link {
  border: none;
  background: none;
  padding: 0;
  color: var(--c-primary);
  cursor: pointer;
  font-size: inherit;
  font-family: inherit;
}

@media (max-width: 768px) {
  .login-wrap {
    grid-template-columns: 1fr;
    margin-top: var(--space-4);
  }
}
</style>
