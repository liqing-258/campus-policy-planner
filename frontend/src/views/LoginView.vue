<script setup lang="ts">
/**
 * 登录页。
 *
 * 布局参照已部署的「智能教室设备管控系统」：左边品牌与能力说明，
 * 右边表单 + 角色卡片（点一下填好演示账号）。
 * 左下角那块"系统规模"读的是 `/health`，是真实计数——不是写死的营销数字。
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, type Health } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('student')
const password = ref('student123')
const health = ref<Health | null>(null)

const features = [
  '解答自带编号引用，点开即见原文与页码',
  '资格判定分「符合 / 不符合 / 无法判定」，缺什么会明说',
  '计划里的每个截止日期都能追回来源条款',
  '同一政策存在新旧两版时并列提示，不替你选',
]

onMounted(async () => {
  if (await auth.restore()) router.replace({ name: 'dashboard' })
  try {
    health.value = await api.get<Health>('/health')
  } catch {
    /* 后端未起时只是不显示规模，不阻挡登录表单渲染 */
  }
})

async function submit() {
  if (await auth.login(username.value.trim(), password.value)) {
    const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.replace(target)
  }
}

type Account = { user: string; pass: string; role: string; desc: string }

const accounts: Account[] = [
  { user: 'student', pass: 'student123', role: '学生', desc: '提问 · 查资格 · 办事务' },
  { user: 'admin', pass: 'admin123', role: '管理员', desc: '维护政策 · 复核规则' },
]

function fill(account: Account) {
  username.value = account.user
  password.value = account.pass
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <section class="left">
        <div class="brand">
          <span class="logo">知</span>
          <div>
            <h1>知校</h1>
            <p class="muted small">校园政策理解与智能事务规划系统</p>
          </div>
        </div>

        <p class="lead">
          把散在文件里的规定，变成你能直接照做的步骤：
          <strong>找到依据 → 判断条件 → 生成计划 → 跟踪进度</strong>。
        </p>

        <ul class="features">
          <li v-for="text in features" :key="text">
            <i>▸</i><span>{{ text }}</span>
          </li>
        </ul>

        <div class="scale">
          <template v-if="health">
            <span class="dim">当前知识库</span>
            <b>{{ health.document_count }}</b>
            <span class="dim">份政策文件 ·</span>
            <b>{{ health.chunk_count }}</b>
            <span class="dim">个可检索片段</span>
          </template>
          <span v-else class="dim">正在读取知识库规模…</span>
        </div>

        <p class="foot dim tiny">
          本系统为课程项目，账号与政策文件均为虚构数据，不作为任何实际办事依据。
        </p>
      </section>

      <section class="right">
        <form @submit.prevent="submit">
          <h3>登录</h3>
          <p class="muted tiny tip">点下方角色卡片可一键填入演示账号</p>

          <div class="field">
            <label for="u">用户名</label>
            <input id="u" v-model="username" autocomplete="username" />
          </div>
          <div class="field">
            <label for="p">密码</label>
            <input id="p" v-model="password" type="password" autocomplete="current-password" />
          </div>

          <p v-if="auth.error" class="err">{{ auth.error }}</p>

          <button class="btn submit" type="submit" :disabled="auth.loading">
            {{ auth.loading ? '登录中…' : '登 录' }}
          </button>
        </form>

        <div class="roles">
          <button
            v-for="account in accounts"
            :key="account.user"
            type="button"
            class="role-chip"
            @click="fill(account)"
          >
            <b>{{ account.role }}</b>
            <span>{{ account.user }} / {{ account.pass }} · {{ account.desc }}</span>
          </button>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-6);
  background:
    radial-gradient(1200px 600px at 50% -10%, #1b2334, #0f1115 60%),
    var(--c-bg);
}

.login-card {
  display: grid;
  grid-template-columns: 1.15fr 0.85fr;
  width: 100%;
  max-width: 880px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 16px;
  box-shadow: var(--shadow-card);
  overflow: hidden;
}

.left {
  padding: 36px 34px;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  min-width: 0;
}

.right {
  padding: 32px 30px;
  border-left: 1px solid var(--c-border);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  min-width: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo {
  width: 40px;
  height: 40px;
  border-radius: 11px;
  background: var(--c-primary);
  color: #fff;
  font-size: 20px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
}

.brand h1 {
  font-size: 22px;
  line-height: 1.2;
}

.brand p {
  margin: 2px 0 0;
}

.lead {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.9;
  color: var(--c-text-2);
}

.lead strong {
  color: var(--c-text);
}

.features {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.features li {
  display: flex;
  gap: 8px;
  font-size: 12.5px;
  color: var(--c-text-2);
  line-height: 1.7;
}

.features i {
  color: var(--c-primary);
  font-style: normal;
}

.scale {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  padding: 9px 12px;
  font-size: 12px;
}

.scale b {
  color: var(--c-primary);
  font-size: 15px;
}

.foot {
  margin: auto 0 0;
  line-height: 1.7;
}

.right h3 {
  margin: 0;
  font-size: 15px;
}

.tip {
  margin: 0 0 var(--space-3);
}

form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.submit {
  width: 100%;
  min-height: 40px;
  margin-top: var(--space-1);
}

.err {
  margin: 0;
  color: var(--c-not-met);
  font-size: 12.5px;
}

.roles {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  border-top: 1px solid var(--c-border);
  padding-top: var(--space-3);
  margin-top: auto;
}

.role-chip {
  display: flex;
  flex-direction: column;
  gap: 1px;
  text-align: left;
  padding: 8px 11px;
  border: 1px solid var(--c-border-strong);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
  color: var(--c-text);
  cursor: pointer;
  font-family: inherit;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.role-chip:hover {
  border-color: var(--c-primary);
  background: var(--c-primary-weak);
}

.role-chip b {
  font-size: 12.5px;
  font-weight: 600;
}

.role-chip span {
  font-size: 11px;
  color: var(--c-text-2);
}

@media (max-width: 780px) {
  .login-card {
    grid-template-columns: 1fr;
  }
  .right {
    border-left: none;
    border-top: 1px solid var(--c-border);
  }
  .left {
    padding: 26px 22px;
  }
  .right {
    padding: 22px;
  }
}
</style>
