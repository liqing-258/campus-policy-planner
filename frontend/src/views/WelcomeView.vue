<script setup lang="ts">
/**
 * 落地页：进站第一屏只有三样东西——系统名、一句话、一个按钮。
 *
 * ## 为什么把登录做成"从下往上滑的小窗"
 *
 * 旧版 `/login` 是一屏左右分栏的大卡片，左边塞了四条能力说明和知识库规模。
 * 那些字其实没人读——用户来这里是办事的，不是来听产品介绍的。
 * 把首屏让给系统名，登录变成用户主动唤起的动作，视觉上也把"公共信息"
 * 与"身份输入"分成了两层：没点按钮之前，页面上没有任何可填的东西。
 *
 * ## 动画方向选"自上而下出现"还是"自下而上"
 *
 * 需求写的是"从页面中间下方向上平滑出现"：面板初始下沉到视口下方，
 * 用 translateY(72px) → 0 收上来。**不用 scale 从小放大**——放大动画
 * 会让文字在途中重排一次，中途截屏会糊；纯位移不会。
 *
 * ## 关闭的三条路
 *
 * 遮罩点击（只认 `self`，否则面板里的点击会冒泡上来把它关掉）、Esc、
 * 右上角 ×。三条都要有：只给一条的话，用键盘的人退不出去。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, type Health } from '../api/client'
import { useAuthStore } from '../stores/auth'
import AuthPanel from '../components/AuthPanel.vue'
import LogoMark from '../components/LogoMark.vue'
import ParticleField from '../components/ParticleField.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

/**
 * `autoOpen` 让 `/login` 这条老路由直接拉开小窗。
 * 用 prop 而不是读 `route.name === 'login'`：LoginView 就是包着 prop 的一层壳，
 * 谁要复用落地页都只需要传一个布尔值，不用记路由名。
 */
const props = withDefaults(defineProps<{ autoOpen?: boolean }>(), { autoOpen: false })

const panelOpen = ref(false)
const health = ref<Health | null>(null)

/** 已登录的话不该看到落地页：直接按 redirect 参数走人 */
const redirectTarget = computed(() =>
  typeof route.query.redirect === 'string' ? route.query.redirect : '/',
)

onMounted(async () => {
  if (await auth.restore()) {
    router.replace(redirectTarget.value)
    return
  }
  if (props.autoOpen) panelOpen.value = true
  try {
    health.value = await api.get<Health>('/health')
  } catch {
    /* 后端没起时不显示规模，也不挡住"开启系统"——登录失败时面板里会报错 */
  }
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
})

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && panelOpen.value) panelOpen.value = false
}

function open() {
  panelOpen.value = true
}

/** 遮罩自己才算"点外面"：面板内部点击会冒泡到遮罩，不加 self 判断会误关 */
function onMaskClick(event: MouseEvent) {
  if (event.target === event.currentTarget) panelOpen.value = false
}
</script>

<template>
  <div class="welcome" :class="{ blurred: panelOpen }">
    <!-- 背景：粒子层在最底（会连成网状），光斑压在它上面做柔化，
         两层都不接收指针事件 -->
    <ParticleField />

    <div class="aura" aria-hidden="true">
      <span class="blob b1" />
      <span class="blob b2" />
      <span class="grid-lines" />
    </div>

    <main class="hero">
      <p class="eyebrow">校园政策理解与事务规划</p>

      <h1 class="brand-row">
        <LogoMark class="brand-logo" :size="86" />
        <span class="title">
          <span class="ch">知</span><span class="ch">校</span>
        </span>
      </h1>

      <p class="tagline">
        把散在文件里的规定，变成你能照着走完的步骤——
        <b>找到依据 · 判断条件 · 生成计划 · 跟踪进度</b>
      </p>

      <button class="start" type="button" @click="open">
        <span>开启系统</span>
        <i aria-hidden="true">→</i>
      </button>

      <p v-if="health" class="scale">
        知识库已收录 <b>{{ health.document_count }}</b> 份政策 ·
        <b>{{ health.chunk_count }}</b> 个可检索片段
      </p>
      <p v-else class="scale dim">正在读取知识库规模…</p>
    </main>

    <footer class="foot dim tiny">
      课程项目 · 账号与政策文件均为虚构数据，不作为任何实际办事依据
    </footer>

    <!-- 登录小窗。遮罩用 v-if 而不是 v-show：关闭时要能打断动画、释放焦点 -->
    <Transition name="mask">
      <div v-if="panelOpen" class="mask" @click="onMaskClick">
        <Transition name="panel" appear>
          <AuthPanel @close="panelOpen = false" @done="panelOpen = false" />
        </Transition>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.welcome {
  position: relative;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--space-8) var(--space-6);
  overflow: hidden;
}

/* 开窗时把整页往后退一格：景深感靠这一层，不靠把遮罩调更黑 */
.welcome.blurred .hero,
.welcome.blurred .foot {
  filter: blur(3px) saturate(0.75);
  opacity: 0.55;
  transform: scale(0.985);
}

.hero {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: var(--space-3);
  max-width: 620px;
  transition: filter 0.35s ease, opacity 0.35s ease, transform 0.35s ease;
}

.eyebrow {
  margin: 0;
  font-size: 12px;
  letter-spacing: 5px;
  text-indent: 5px;
  /* 不能用 --c-dim：R11 起页面背景是渐变，eyebrow 正落在顶部光晕的
     合成底上，dim 实测只剩 4.49:1（差 0.01 达标）。全站 dim 文字里
     只有这一处直接坐在渐变上，其余都在卡片/侧栏的白底里。
     用 --c-text-2 实测 4.98:1，语义上也更对——眉题是"次级文字"不是"弱化文字" */
  color: var(--c-text-2);
  text-transform: uppercase;
}

/* 系统名与 logo 并排。logo 比字略小一点（86 vs 108），
   视觉上两者"同高不同级"——图形是陪衬，字才是主体 */
.brand-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: clamp(14px, 2.4vw, 26px);
  margin: 0;
}

.brand-logo {
  /* 与标题的光晕同源，让图形和字"长在同一个光源下" */
  filter: drop-shadow(0 10px 26px rgba(10, 125, 111, 0.22));
  animation: rise 0.72s cubic-bezier(0.22, 0.9, 0.25, 1) both;
}

.title {
  margin: 0;
  display: flex;
  font-size: clamp(64px, 12vw, 108px);
  font-weight: 700;
  line-height: 1.05;
  letter-spacing: 10px;
  /**
   * **不用 `background-clip: text` + `-webkit-text-fill-color: transparent`。**
   *
   * 那套写法在无头 Chromium 里整块字面会消失（实测 rect 是 236×113、文本也在，
   * 但一个像素都不画），在部分老显卡 / 非整数缩放下同样会掉字。
   * 对一个"进站第一眼就要看见系统名"的元素来说这个风险不值得冒。
   *
   * R10 浅底后改为深墨绿实色 + 极淡青影：浅底上白字发光那套完全不成立，
   * 而深字加一层淡影足以把字从粒子背景里"托"出来。
   */
  color: #12302b;
  text-shadow:
    0 1px 0 rgba(255, 255, 255, 0.85),
    0 14px 38px rgba(10, 125, 111, 0.14);
}

.ch {
  display: inline-block;
  animation: rise 0.72s cubic-bezier(0.22, 0.9, 0.25, 1) both;
}

.ch:nth-child(2) {
  animation-delay: 0.11s;
}

/* 逐字上浮：两个字错开 110ms，多一帧就拖沓，少一帧像同时出现 */
@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(22px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.tagline {
  margin: 0;
  font-size: 14px;
  line-height: 2;
  color: var(--c-text-2);
  max-width: 520px;
}

.tagline b {
  color: var(--c-text);
  font-weight: 500;
}

.start {
  margin-top: var(--space-4);
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: 48px;
  padding: 0 30px;
  border-radius: 999px;
  border: 1px solid var(--c-primary-strong);
  background: var(--c-primary-strong);
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  font-family: inherit;
  letter-spacing: 1px;
  cursor: pointer;
  box-shadow: 0 10px 26px rgba(10, 125, 111, 0.26);
  transition: transform 0.18s ease, box-shadow 0.18s ease, filter 0.18s ease;
}

.start i {
  font-style: normal;
  transition: transform 0.18s ease;
}

.start:hover {
  transform: translateY(-2px);
  filter: brightness(1.1);
  box-shadow: 0 14px 34px rgba(10, 125, 111, 0.34);
}

.start:hover i {
  transform: translateX(4px);
}

.start:active {
  transform: translateY(0);
}

.scale {
  margin: var(--space-3) 0 0;
  font-size: 12px;
  color: var(--c-text-2);
}

.scale b {
  color: var(--c-accent);
  font-variant-numeric: tabular-nums;
}

.foot {
  position: absolute;
  bottom: var(--space-4);
  left: 0;
  right: 0;
  text-align: center;
  transition: filter 0.35s ease, opacity 0.35s ease;
}

/* ---------- 背景光斑 ---------- */
.aura {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: hidden;
}

.blob {
  position: absolute;
  border-radius: 50%;
  filter: blur(90px);
  opacity: 0.5;
}

.b1 {
  width: 520px;
  height: 520px;
  left: 8%;
  top: -14%;
  background: rgba(10, 125, 111, 0.09);
  animation: drift 22s ease-in-out infinite alternate;
}

.b2 {
  width: 420px;
  height: 420px;
  right: 6%;
  bottom: -16%;
  background: rgba(156, 100, 16, 0.05);
  animation: drift 27s ease-in-out infinite alternate-reverse;
}

@keyframes drift {
  from {
    transform: translate3d(0, 0, 0);
  }
  to {
    transform: translate3d(38px, 26px, 0);
  }
}

/* 极淡的方格：给大面积留白一点"纸"的质感，密度低了看起来像脏点，
   所以格距定 72px、透明度压到 0.5 */
.grid-lines {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(to right, rgba(18, 48, 43, 0.045) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(18, 48, 43, 0.045) 1px, transparent 1px);
  background-size: 72px 72px;
  mask-image: radial-gradient(circle at 50% 42%, #000 0%, transparent 70%);
  -webkit-mask-image: radial-gradient(circle at 50% 42%, #000 0%, transparent 70%);
}

/* ---------- 遮罩与出入场 ---------- */
.mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgba(18, 48, 43, 0.28);
  backdrop-filter: blur(2px);
}

.mask-enter-active,
.mask-leave-active {
  transition: opacity 0.28s ease;
}

.mask-enter-from,
.mask-leave-to {
  opacity: 0;
}

/* 需求原话是"从页面中间下方向上平滑出现"：起点在中心下方 72px。
   曲线前半段快、后半段缓，收尾不会"顶一下"。 */
.panel-enter-active {
  transition: transform 0.42s cubic-bezier(0.22, 0.9, 0.25, 1), opacity 0.3s ease;
}

.panel-leave-active {
  transition: transform 0.2s ease, opacity 0.2s ease;
}

.panel-enter-from {
  opacity: 0;
  transform: translateY(72px) scale(0.98);
}

.panel-leave-to {
  opacity: 0;
  transform: translateY(28px) scale(0.99);
}

@media (max-width: 640px) {
  .title {
    letter-spacing: 6px;
  }
  .tagline {
    font-size: 13px;
  }
  .start {
    min-height: 44px;
    padding: 0 24px;
  }
}
</style>
