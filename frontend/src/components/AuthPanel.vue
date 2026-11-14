<script setup lang="ts">
/**
 * 登录 / 注册面板。落地页点「开启系统」后它从下方滑上来，本身不负责遮罩与动画。
 *
 * ## 两行选择的顺序不是随意的
 *
 * 第一行「学生端 / 管理端」，第二行「登录 / 注册」——**先定身份，再定动作**。
 * 反过来的话，用户在"注册"里填完三栏才发现自己选错了端，然后要重填。
 * 而且两端的能力不同（管理员不开放自助注册），这个顺序让"选端"顺带把
 * 可用动作也定下来了：选管理端时注册页直接变成一句说明，而不是让他填完再报错。
 *
 * ## 密码为什么是选填
 *
 * 需求给的注册字段是「学生姓名 / 学号 / 数字验证码」三栏。但登录必须有密码，
 * 所以留空时用学号后 6 位，并在注册成功那一屏**把初始密码原样写出来**。
 * 让用户自己去猜"默认密码是什么"，是这类课设系统最常见的坑。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Captcha, type RegisterResult } from '../api/client'
import { useAuthStore } from '../stores/auth'
import LogoMark from './LogoMark.vue'

const emit = defineEmits<{ close: []; done: [] }>()

const auth = useAuthStore()
const router = useRouter()

type Role = 'student' | 'admin'
type Mode = 'login' | 'register'

/** v-for 里直接写对象数组会把类型推断丢掉，模板里还要 `as Role`，索性提到脚本里 */
const ROLE_TABS: { key: Role; label: string }[] = [
  { key: 'student', label: '学生端' },
  { key: 'admin', label: '管理端' },
]

const role = ref<Role>('student')
const mode = ref<Mode>('login')
const localError = ref('')
const captcha = ref<Captcha | null>(null)
const captchaLoading = ref(false)
/** 注册成功那一屏：把初始密码摆在用户眼前 */
const registered = ref<RegisterResult | null>(null)

const loginForm = reactive({ username: 'student', password: 'student123' })
const registerForm = reactive({
  display_name: '',
  student_no: '',
  password: '',
  captcha_code: '',
})

const roleLabel = computed(() => (role.value === 'admin' ? '管理端' : '学生端'))
const accountLabel = computed(() => (role.value === 'admin' ? '用户名' : '学号 / 用户名'))
/** 管理员账号由校方下发：这一栏不是"禁用"，而是根本不该有 */
const canRegister = computed(() => role.value === 'student')

watch(role, () => {
  localError.value = ''
  auth.error = ''
  registered.value = null
  if (role.value === 'admin') {
    loginForm.username = 'admin'
    loginForm.password = 'admin123'
  } else {
    loginForm.username = 'student'
    loginForm.password = 'student123'
  }
})

watch(mode, (next) => {
  localError.value = ''
  auth.error = ''
  if (next === 'register' && !captcha.value) void refreshCaptcha()
})

onMounted(() => {
  // 直接落在 /login 时先取一张码：切到注册页不用再等一次网络
  void refreshCaptcha()
})

async function refreshCaptcha() {
  captchaLoading.value = true
  try {
    captcha.value = await api.get<Captcha>('/auth/captcha')
    registerForm.captcha_code = ''
  } catch {
    captcha.value = null
    localError.value = '验证码加载失败，请检查后端服务'
  } finally {
    captchaLoading.value = false
  }
}

/** 只收数字，最多四位。粘贴一长串也只会留下前四位 */
function onCaptchaInput(event: Event) {
  const input = event.target as HTMLInputElement
  const digits = input.value.replace(/\D/g, '').slice(0, 4)
  input.value = digits
  registerForm.captcha_code = digits
}

/** 学号只收字母数字：中文姓名误填进学号栏是最常见的手误 */
function onStudentNoInput(event: Event) {
  const input = event.target as HTMLInputElement
  const cleaned = input.value.replace(/[^0-9A-Za-z]/g, '').slice(0, 20)
  input.value = cleaned
  registerForm.student_no = cleaned
}

async function submitLogin() {
  localError.value = ''
  if (!loginForm.username.trim() || !loginForm.password) {
    localError.value = '请填写账号与密码'
    return
  }
  if (await auth.login(loginForm.username.trim(), loginForm.password)) {
    emit('done')
    router.replace('/')
  }
}

async function submitRegister() {
  localError.value = ''
  if (!canRegister.value) return
  if (registerForm.display_name.trim().length < 2) {
    localError.value = '请填写真实姓名（至少 2 个字）'
    return
  }
  if (registerForm.student_no.length < 4) {
    localError.value = '学号至少 4 位'
    return
  }
  if (registerForm.password && registerForm.password.length < 6) {
    localError.value = '密码至少 6 位，或留空由系统生成'
    return
  }
  if (!captcha.value) {
    localError.value = '验证码未加载，请点击图片重试'
    return
  }
  if (registerForm.captcha_code.length !== 4) {
    localError.value = '请输入 4 位数字验证码'
    return
  }

  const result = await auth.register({
    display_name: registerForm.display_name.trim(),
    student_no: registerForm.student_no,
    // 空串不能传：后端把 "" 当"没填"，但接口契约里可选字段应当直接不出现
    ...(registerForm.password ? { password: registerForm.password } : {}),
    captcha_id: captcha.value.captcha_id,
    captcha_code: registerForm.captcha_code,
  })

  if (result) {
    registered.value = result
    return
  }
  // 注册失败也必须换码：服务端是**先校验后查重**，验过一次这张码就作废了
  // （重名、密码太短这些"业务失败"同样走完了校验）。不换的话用户拿着废码点第二次，
  // 只会看到"验证码已失效"，还以为自己打错了。
  void refreshCaptcha()
}

function enterSystem() {
  emit('done')
  router.replace('/')
}

function fillDemo(which: 'student' | 'admin') {
  role.value = which
  mode.value = 'login'
  loginForm.username = which === 'admin' ? 'admin' : 'student'
  loginForm.password = which === 'admin' ? 'admin123' : 'student123'
}
</script>

<template>
  <div class="auth">
    <button class="close" type="button" title="关闭" aria-label="关闭" @click="emit('close')">
      ×
    </button>

    <header class="head">
      <LogoMark :size="44" />
      <div class="head-text">
        <strong>知校</strong>
        <small>校园政策理解与事务规划系统</small>
      </div>
    </header>

    <!-- 第一行：身份 -->
    <div class="seg" role="tablist" aria-label="选择端口">
      <button
        v-for="item in ROLE_TABS"
        :key="item.key"
        type="button"
        class="seg-btn"
        :class="{ on: role === item.key }"
        role="tab"
        :aria-selected="role === item.key"
        :data-role="item.key"
        @click="role = item.key"
      >
        {{ item.label }}
      </button>
    </div>

    <!-- 第二行：动作 -->
    <div class="seg tabs" role="tablist" aria-label="登录或注册">
      <button
        type="button"
        class="seg-btn"
        :class="{ on: mode === 'login' }"
        role="tab"
        :aria-selected="mode === 'login'"
        data-mode="login"
        @click="mode = 'login'"
      >
        登录
      </button>
      <button
        type="button"
        class="seg-btn"
        :class="{ on: mode === 'register' }"
        role="tab"
        :aria-selected="mode === 'register'"
        data-mode="register"
        @click="mode = 'register'"
      >
        注册
      </button>
    </div>

    <!-- ---------- 注册成功 ---------- -->
    <div v-if="registered" class="done">
      <p class="done-title">注册成功，已为你登录</p>
      <dl class="kv">
        <dt>姓名</dt>
        <dd>{{ registered.user.display_name }}</dd>
        <dt>学号</dt>
        <dd class="mono">{{ registered.user.username }}</dd>
        <dt>初始密码</dt>
        <dd>
          <template v-if="registered.initial_password">
            <b class="pw mono">{{ registered.initial_password }}</b>
            <span class="dim tiny">（学号后 6 位，请记下）</span>
          </template>
          <span v-else class="dim tiny">你设置的密码</span>
        </dd>
      </dl>
      <button class="btn submit" type="button" @click="enterSystem">进入系统</button>
    </div>

    <!-- ---------- 登录 ---------- -->
    <form v-else-if="mode === 'login'" class="form" @submit.prevent="submitLogin">
      <p class="hint">
        {{ role === 'admin' ? '使用校方下发的管理员账号登录' : '用学号或演示账号登录' }}
      </p>

      <div class="field">
        <label for="auth-user">{{ accountLabel }}</label>
        <input id="auth-user" v-model="loginForm.username" autocomplete="username" />
      </div>
      <div class="field">
        <label for="auth-pass">密码</label>
        <input
          id="auth-pass"
          v-model="loginForm.password"
          type="password"
          autocomplete="current-password"
        />
      </div>

      <p v-if="localError || auth.error" class="err">{{ localError || auth.error }}</p>

      <button class="btn submit" type="submit" :disabled="auth.loading">
        {{ auth.loading ? '登录中…' : `登录${roleLabel}` }}
      </button>

      <div class="demo">
        <span class="demo-label">演示账号</span>
        <div class="demo-grid">
          <button type="button" class="demo-chip" @click="fillDemo('student')">
            <span class="chip-role">学生</span>
            <span class="chip-cred mono">student / student123</span>
          </button>
          <button type="button" class="demo-chip" @click="fillDemo('admin')">
            <span class="chip-role">管理员</span>
            <span class="chip-cred mono">admin / admin123</span>
          </button>
        </div>
      </div>
    </form>

    <!-- ---------- 注册 ---------- -->
    <form v-else-if="canRegister" class="form" @submit.prevent="submitRegister">
      <p class="hint">学生自助注册：姓名 + 学号 + 数字验证码</p>

      <div class="field">
        <label for="reg-name">学生姓名</label>
        <input id="reg-name" v-model="registerForm.display_name" placeholder="如：张明" autocomplete="name" />
      </div>
      <div class="field">
        <label for="reg-no">学号</label>
        <input
          id="reg-no"
          :value="registerForm.student_no"
          placeholder="如：240123020027"
          inputmode="numeric"
          autocomplete="username"
          @input="onStudentNoInput"
        />
      </div>
      <div class="field">
        <label for="reg-pw">密码 <span class="dim tiny">选填，留空则用学号后 6 位</span></label>
        <input
          id="reg-pw"
          v-model="registerForm.password"
          type="password"
          placeholder="至少 6 位，可留空"
          autocomplete="new-password"
        />
      </div>

      <div class="field">
        <label for="reg-code">数字验证码</label>
        <div class="captcha-row">
          <input
            id="reg-code"
            :value="registerForm.captcha_code"
            placeholder="4 位数字"
            inputmode="numeric"
            maxlength="4"
            @input="onCaptchaInput"
          />
          <button
            type="button"
            class="captcha-img"
            title="看不清？点击换一张"
            aria-label="换一张验证码"
            :disabled="captchaLoading"
            @click="refreshCaptcha"
          >
            <img v-if="captcha" :src="captcha.image" alt="数字验证码" />
            <span v-else class="dim tiny">{{ captchaLoading ? '加载中…' : '点击重试' }}</span>
          </button>
        </div>
      </div>

      <p v-if="localError || auth.error" class="err">{{ localError || auth.error }}</p>

      <button class="btn submit" type="submit" :disabled="auth.loading">
        {{ auth.loading ? '注册中…' : '注册并登录' }}
      </button>
      <p class="dim tiny foot">注册即使用学号作为登录账号；管理员账号不开放自助注册。</p>
    </form>

    <div v-else class="admin-note">
      <p><strong>管理端不开放自助注册</strong></p>
      <p class="muted small">管理员账号由校方统一下发。若你是学生，请切到「学生端」注册。</p>
      <button type="button" class="btn btn-ghost btn-sm" @click="role = 'student'">切到学生端</button>
    </div>
  </div>
</template>

<style scoped>
.auth {
  position: relative;
  width: min(420px, calc(100vw - 32px));
  padding: 26px 26px 22px;
  border-radius: 18px;
  border: 1px solid var(--c-border);
  background: var(--c-surface);
  box-shadow: var(--shadow-pop);
  color: var(--c-text);
  backdrop-filter: blur(14px);
}

.close {
  position: absolute;
  top: 10px;
  right: 12px;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--c-text-2);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.close:hover {
  background: var(--c-elev-2);
  color: var(--c-text);
}

.head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}

.head-text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  min-width: 0;
}

.head-text strong {
  font-size: 19px;
  letter-spacing: 2px;
}

.head-text small {
  font-size: 11.5px;
  color: var(--c-text-2);
}

.seg {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  padding: 4px;
  border-radius: 11px;
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  margin-bottom: 10px;
}

.seg.tabs {
  margin-bottom: 18px;
}

.seg-btn {
  min-height: 34px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--c-text-2);
  font-size: 13.5px;
  font-family: inherit;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
}

.seg-btn:hover {
  color: var(--c-text);
}

.seg-btn.on {
  background: var(--c-primary-strong);
  color: #fff;
  box-shadow: var(--shadow-card);
}

.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.hint {
  margin: 0 0 2px;
  font-size: 12.5px;
  color: var(--c-text-2);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field label {
  font-size: 12px;
  color: var(--c-text-2);
}

.field input {
  min-height: 40px;
  padding: 0 12px;
  border-radius: 9px;
  border: 1px solid var(--c-border-strong);
  background: var(--c-surface);
  color: var(--c-text);
  font-size: 13.5px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}

.field input::placeholder {
  color: var(--c-dim);
}

.field input:focus {
  border-color: var(--c-primary);
  background: var(--c-surface);
  box-shadow: 0 0 0 3px var(--c-primary-weak);
}

.captcha-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 116px;
  gap: 10px;
  align-items: stretch;
}

.captcha-img {
  padding: 0;
  border: 1px solid var(--c-border-strong);
  border-radius: 9px;
  background: var(--c-surface);
  cursor: pointer;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
}

.captcha-img img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.submit {
  margin-top: 4px;
  min-height: 42px;
  width: 100%;
  border-radius: 10px;
  border: 1px solid var(--c-primary-strong);
  background: var(--c-primary-strong);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition: filter 0.16s ease, transform 0.16s ease;
}

.submit:hover:not(:disabled) {
  filter: brightness(1.12);
  transform: translateY(-1px);
}

.submit:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.err {
  margin: 0;
  padding: 7px 10px;
  border-radius: 8px;
  background: var(--c-not-met-bg);
  border: 1px solid var(--c-not-met-border);
  color: var(--c-not-met);
  font-size: 12.5px;
}

.demo {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 12px;
  margin-top: 2px;
  border-top: 1px solid var(--c-border);
}

/* 标签单独一行、两枚 chip 等分一行。
   原来用 flex-wrap 平铺，标签和 chip 挤在同一行时会因文字宽度不同
   而错位（截图里"演示账号"缩在左侧、两个 chip 换行后左边缘对不齐）。
   标签独占一行 + 网格等分，左右边缘天然对齐，也不受账号字符串长度影响。 */
.demo-label {
  font-size: 11.5px;
  color: var(--c-dim);
}

.demo-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.demo-chip {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  padding: 7px 10px;
  border-radius: var(--radius-control);
  border: 1px solid var(--c-border-strong);
  background: var(--c-surface-2);
  color: var(--c-text-2);
  font-size: 11.5px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
}

.demo-chip:hover {
  border-color: var(--c-primary-border);
  background: var(--c-primary-weak);
  color: var(--c-primary);
}

.chip-role {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--c-text);
}

.demo-chip:hover .chip-role {
  color: var(--c-primary);
}

.chip-cred {
  font-size: 10.5px;
  letter-spacing: -0.2px;
  color: var(--c-dim);
  white-space: nowrap;
}

.foot {
  margin: 0;
  text-align: center;
  line-height: 1.6;
}

/* ---------- 注册成功 ---------- */
.done {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.done-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--c-met);
}

.done .kv {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 8px 12px;
  margin: 0;
  font-size: 13px;
}

.done .kv dt {
  color: var(--c-text-2);
}

.done .kv dd {
  margin: 0;
}

.pw {
  color: var(--c-accent);
  font-size: 15px;
  letter-spacing: 1px;
}

.admin-note {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px;
  border-radius: 12px;
  border: 1px dashed var(--c-border-strong);
  background: var(--c-surface-2);
}

.admin-note p {
  margin: 0;
}

.admin-note .btn {
  align-self: flex-start;
}
</style>
