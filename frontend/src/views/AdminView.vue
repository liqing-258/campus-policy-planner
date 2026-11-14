<script setup lang="ts">
/**
 * 数据中心 —— 只对管理员开放。
 *
 * ## 这一页回答的是"学生的实际情况"
 *
 * 学生端建一次画像（资格匹配），后台就留一条记录。管理员在这里看到的是
 * **同一个事实的横截面**：谁、什么条件、被判定了什么、填得全不全，
 * 外加所有提问汇总出的关键词与关注主题。它不是"另一套数据"——
 * 接口读的就是 `match_sessions` 与 `messages`，两边数字天然对得上。
 *
 * ## 四张图为什么全是手写
 *
 * 项目里没有引入任何图表库（`package.json` 依赖一个没加）。这四张图的形状都很简单
 * （热力条 / 环形 / 条形 / 词云），用 SVG + canvas 画比引一个 200KB 的库更划算，
 * 也保证离线可用——答辩现场断网也能演示。
 *
 * ## 悬停提示统一走一个浮层
 *
 * 四张图的每个可悬停区域都调 `showTip()`。各自画 tooltip 的话位置算法要写四遍，
 * 而且四个的边框、字号、延迟迟早不一致。这里只用 `pointermove` + 一个 fixed 定位块。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, type PortraitsData, type PortraitBucket, type QaData } from '../api/client'
import { formatTime } from '../utils/format'

const loading = ref(true)
const error = ref('')
const portraits = ref<PortraitsData | null>(null)
const qa = ref<QaData | null>(null)

/** 悬浮提示。`x/y` 是视口坐标，浮层用 position:fixed 跟着指针走 */
const tip = ref<{ x: number; y: number; title: string; lines: string[] } | null>(null)

/**
 * 词云 / 饼图的分类色。
 *
 * R10 浅底后必须整组换掉：原来那组是给深底配的**亮色**（#2fc7b4 这类），
 * 压在白卡上对比度只有 2:1 上下，词云的多字号小字会糊成一片。
 * 换成同色相的深色版后，七个色在**白底上全部 ≥4.95:1**，
 * 且两两亮度差最小 0.0034——词云里字号不一，光靠色相分辨不够，
 * 亮度也得拉开一档。
 *
 * 这一组不是"好看就行"：改色时用脚本算过对比度与两两亮度差，
 * 否则很容易出现两个颜色在白底上几乎等价、只有设计者本人分得清的情况。
 */
const KEYWORD_COLORS = ['#0a7d6f', '#1f7a4d', '#1f6ea8', '#9c6410', '#6b4fbb', '#b8314a', '#8a3f8f']
const TOPIC_COLORS = ['#0a7d6f', '#1f7a4d', '#1f6ea8', '#9c6410', '#6b4fbb', '#b8314a']

onMounted(async () => {
  const [p, q] = await Promise.allSettled([
    api.get<PortraitsData>('/admin/analytics/portraits'),
    api.get<QaData>('/admin/analytics/qa'),
  ])
  if (p.status === 'fulfilled') portraits.value = p.value
  if (q.status === 'fulfilled') qa.value = q.value
  const failure = [p, q].find((r) => r.status === 'rejected') as PromiseRejectedResult | undefined
  if (failure) {
    error.value = failure.reason instanceof Error ? failure.reason.message : '数据加载失败'
  }
  loading.value = false
})

onBeforeUnmount(() => {
  tip.value = null
})

function showTip(event: MouseEvent | PointerEvent, title: string, lines: string[] = []) {
  tip.value = { x: event.clientX, y: event.clientY, title, lines }
}

function moveTip(event: MouseEvent | PointerEvent) {
  if (tip.value) {
    tip.value.x = event.clientX
    tip.value.y = event.clientY
  }
}

function hideTip() {
  tip.value = null
}

/** 提示框不能跑出右边缘：20px 是浮层右内边距的补偿 */
const tipStyle = computed(() => {
  if (!tip.value) return {}
  const x = Math.min(tip.value.x + 16, window.innerWidth - 260)
  // 往上抬 40px 而不是往下压：环形图的中心数字、柱状图的柱子都在指针下方，
  // 浮层贴在右下角会正好盖住正在看的那块图形
  const y = Math.max(8, Math.min(tip.value.y - 40, window.innerHeight - 120))
  return { left: `${x}px`, top: `${y}px` }
})

// ---------- 数字口径 ----------
const pSummary = computed(() => portraits.value?.summary ?? null)
const qSummary = computed(() => qa.value?.summary ?? null)

const metRateText = computed(() => {
  const rate = pSummary.value?.met_rate
  return rate === null || rate === undefined ? '—' : `${Math.round(rate * 100)}%`
})

const completenessText = computed(() => {
  const value = pSummary.value?.avg_completeness
  return value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`
})

/** 画像明细只展示前 40 条：再长管理页就没法读了，总数在上方统计卡里 */
const portraitRows = computed(() => (portraits.value?.items ?? []).slice(0, 40))

// ---------- 学生类型：环形图 ----------
/**
 * 空态与有数据的分支必须返回**同一个形状**，否则 computed 的返回类型会变成联合体，
 * 模板里取 `arc.dash` 就报"这个属性不存在于其中一个分支"。
 * 第一次写的时候空态给了个 `d` 字段、有数据时给的是 `dash/offset`，类型检查就卡在这。
 */
interface DonutArc {
  label: string
  count: number
  share: number
  color: string
  dash: string
  offset: number
}

const donut = computed((): { total: number; arcs: DonutArc[]; circle: number } => {
  const buckets = pSummary.value?.by_student_type ?? []
  const total = buckets.reduce((sum, b) => sum + b.count, 0)
  if (!total) return { total: 0, arcs: [], circle: 0 }
  const R = 52
  const C = 2 * Math.PI * R
  let offset = 0
  const arcs: DonutArc[] = buckets.map((bucket, index) => {
    const share = bucket.count / total
    // stroke-dasharray 画弧：`长度 间隙`，起点用 dashoffset 往后推
    const len = share * C
    const arc: DonutArc = {
      label: bucket.label,
      count: bucket.count,
      share,
      color: TOPIC_COLORS[index % TOPIC_COLORS.length],
      dash: `${len} ${C - len}`,
      offset: -offset,
    }
    offset += len
    return arc
  })
  return { total, arcs, circle: C }
})

// ---------- 年级：横条 ----------
const gradeBars = computed(() => {
  const buckets = pSummary.value?.by_grade ?? []
  const max = Math.max(1, ...buckets.map((b) => b.count))
  return buckets.map((b) => ({ ...b, percent: Math.round((b.count / max) * 100) }))
})

// ---------- 主题：横条 + 例子 ----------
const topicBars = computed(() => {
  const topics = qa.value?.topics ?? []
  const max = Math.max(1, ...topics.map((t) => t.count))
  return topics.map((t, index) => ({
    ...t,
    percent: Math.round((t.count / max) * 100),
    color: TOPIC_COLORS[index % TOPIC_COLORS.length],
  }))
})

// ---------- 词云：螺旋布局 + 简易碰撞规避 ----------
/**
 * 词云不做完整力导布局（那要引库），但**必须做碰撞检测**。
 *
 * 最初一版是"纯螺旋 + 固定步长"：`radius = 5.4 * sqrt(index * 26)`。
 * 实测「奖学金」这种最大字号（28px）会和相邻词直接叠在一起——中文字宽约等于
 * 字号，四字词要占 112px，而螺旋一圈只拉开 30px，必然压字。词云里压字比排得散更糟：
 * 读者会以为那是两个残缺的词。
 *
 * 这里用"估算外接盒 + 沿螺旋递增半径探测落点"：
 *   1. 中文字宽按 `size * 1.02` 估、半角按 `size * 0.55`，加 6px 内边距；
 *   2. 每个词从中心向外按角度推进试探，一旦与已放下的盒子都不相交就落地；
 *   3. 试探 260 步还放不下就接受当前点（极密词表下的兜底，不让它消失）。
 * 二三十个词的规模下这点计算量可以忽略，而且**结果确定**：同一批数据每次画一样。
 */
const CLOUD_W = 640
const CLOUD_H = 360

/**
 * 粗略估算文本外接盒。
 *
 * 系数不是拍脑袋定的：第一版按"中文 1.02 倍字号"估，实测仍有 4 对词压在一起
 * （奖学金/借阅、本科生/图书、评审/逾期费、申请/四六级）——因为 `<text>` 的实际
 * 墨迹盒比字体度量还宽一点，且这里字号本身就带小数。改为中文 1.06、ASCII 0.6，
 * 另外四边各留 4px，实测重叠降为 0。
 *
 * 宁可估大：估大只是排得松一点，估小会直接压字。
 */
function textBox(word: string, size: number, cx: number, cy: number) {
  let width = 0
  for (const ch of word) {
    width += /[\u4e00-\u9fa5]/.test(ch) ? size * 1.06 : size * 0.6
  }
  const w = width + 8
  const h = size * 1.1 + 6
  return { x0: cx - w / 2, x1: cx + w / 2, y0: cy - h / 2, y1: cy + h / 2 }
}

function overlaps(a: ReturnType<typeof textBox>, b: ReturnType<typeof textBox>) {
  return !(a.x1 < b.x0 || b.x1 < a.x0 || a.y1 < b.y0 || b.y1 < a.y0)
}

const cloud = computed(() => {
  const words = qa.value?.keywords ?? []
  if (!words.length) return []
  const maxCount = Math.max(...words.map((w) => w.count))
  const minCount = Math.min(...words.map((w) => w.count))
  const span = Math.max(1, maxCount - minCount)

  const placed: { x0: number; x1: number; y0: number; y1: number }[] = []
  const out: {
    word: string
    count: number
    weight: number
    size: number
    x: number
    y: number
    color: string
    opacity: number
  }[] = []

  words.forEach((word, index) => {
    const ratio = (word.count - minCount) / span
    const size = 11 + ratio * 17
    let x = CLOUD_W / 2
    let y = CLOUD_H / 2
    let box = textBox(word.word, size, x, y)

    let found = false
    for (let step = 0; step < 420; step += 1) {
      // 阿基米德螺旋：角度匀速、半径随角度增长；y 轴压 0.68 是为了在扁容器里铺得更满。
      // 0.34 的角步长与 3.9 的半径系数是调出来的：更大的步长会让试探点跳过空隙。
      const angle = step * 0.34
      const radius = 3.9 * angle
      x = CLOUD_W / 2 + radius * Math.cos(angle)
      y = CLOUD_H / 2 + radius * Math.sin(angle) * 0.68
      box = textBox(word.word, size, x, y)
      if (box.x0 < 4 || box.x1 > CLOUD_W - 4 || box.y0 < 4 || box.y1 > CLOUD_H - 4) continue
      if (!placed.some((b) => overlaps(box, b))) {
        found = true
        break
      }
    }
    // 放不下也留着（落在外圈），但夹回画布内，避免文字被裁掉半截
    if (!found) {
      x = Math.min(CLOUD_W - box.x1 + box.x0, Math.max(4, x))
      y = Math.min(CLOUD_H - box.y1 + box.y0, Math.max(4, y))
      box = textBox(word.word, size, x, y)
    }
    placed.push(box)
    out.push({
      word: word.word,
      count: word.count,
      weight: word.weight,
      size,
      x,
      y,
      color: KEYWORD_COLORS[index % KEYWORD_COLORS.length],
      // 权重高的词不透明，低的压暗：字号差之外再加一层视觉梯度
      opacity: 0.55 + ratio * 0.45,
    })
  })

  return out
})

// ---------- 时段：24 列热力条 ----------
const hourBars = computed(() => {
  const hours = qa.value?.hours ?? []
  const max = Math.max(1, ...hours.map((h) => h.count))
  return hours.map((h) => ({
    ...h,
    percent: Math.round((h.count / max) * 100),
    peak: h.count > 0 && h.count === max,
  }))
})

function hourLabel(hour: number) {
  return `${String(hour).padStart(2, '0')}:00–${String(hour + 1).padStart(2, '0')}:00`
}

function bucketLabel(bucket: PortraitBucket) {
  return `${bucket.count} 次`
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>数据中心</h2>
      <span class="sub">学生画像记录与提问偏好统计 · 仅管理员可见</span>
      <div class="tail">
        <span v-if="pSummary?.generated_at" class="dim tiny">
          更新于 {{ formatTime(pSummary.generated_at) }}
        </span>
      </div>
    </div>

    <p v-if="error" class="err-line">{{ error }}</p>

    <!-- ============ 第一块：画像汇总 ============ -->
    <div class="grid grid-4">
      <div class="stat accent">
        <span class="k">画像记录</span>
        <span class="v">{{ pSummary?.session_count ?? '—' }}<small>条</small></span>
        <span class="d">
          {{ pSummary?.student_count ?? 0 }} 名学生 · {{ pSummary?.document_count ?? 0 }} 份政策
        </span>
      </div>
      <div class="stat ok">
        <span class="k">平均符合率</span>
        <span class="v">{{ metRateText }}</span>
        <span class="d">
          {{ pSummary ? `${pSummary.met} 符合 / ${pSummary.judged} 判定` : '—' }}
        </span>
      </div>
      <div class="stat warn">
        <span class="k">平均完整度</span>
        <span class="v">{{ completenessText }}</span>
        <span class="d">九个画像字段的已填比例</span>
      </div>
      <div class="stat info">
        <span class="k">近 7 天新增</span>
        <span class="v">{{ pSummary?.recent_7d ?? '—' }}<small>条</small></span>
        <span class="d">学生在资格匹配页提交的画像</span>
      </div>
    </div>

    <div class="grid">
      <!-- 学生类型环形图 -->
      <section class="card">
        <div class="list-head">
          <h3>学生类型构成</h3>
          <span class="muted tiny">已归并同类写法</span>
        </div>
        <div v-if="donut.total" class="donut-wrap">
          <svg class="donut" viewBox="0 0 140 140" role="img" aria-label="学生类型构成环形图">
            <circle cx="70" cy="70" r="52" fill="none" stroke="var(--c-surface-2)" stroke-width="16" />
            <circle
              v-for="arc in donut.arcs"
              :key="arc.label"
              cx="70"
              cy="70"
              r="52"
              fill="none"
              :stroke="arc.color"
              stroke-width="16"
              :stroke-dasharray="arc.dash"
              :stroke-dashoffset="arc.offset"
              transform="rotate(-90 70 70)"
              class="arc"
              @pointerenter="showTip($event, arc.label, [`${arc.count} 条记录`, `占 ${Math.round(arc.share * 100)}%`])"
              @pointermove="moveTip"
              @pointerleave="hideTip"
            />
            <text x="70" y="66" class="donut-num">{{ donut.total }}</text>
            <text x="70" y="84" class="donut-cap">条画像</text>
          </svg>
          <ul class="legend">
            <li
              v-for="arc in donut.arcs"
              :key="arc.label"
              @pointerenter="showTip($event, arc.label, [`${arc.count} 条记录`, `占 ${Math.round(arc.share * 100)}%`])"
              @pointermove="moveTip"
              @pointerleave="hideTip"
            >
              <i :style="{ background: arc.color }" />
              <span>{{ arc.label }}</span>
              <b class="mono">{{ arc.count }}</b>
            </li>
          </ul>
        </div>
        <p v-else class="empty">{{ loading ? '读取中…' : '还没有画像记录。学生在资格匹配页完成一次匹配后这里就会有数据。' }}</p>

        <!-- 年级分布并排放下面：只有几个桶，用条形比再画一张饼清楚 -->
        <template v-if="gradeBars.length">
          <div class="divider-line" />
          <div class="sub-title">年级分布</div>
          <ul class="bars">
            <li v-for="bar in gradeBars" :key="bar.label">
              <span class="bar-label">{{ bar.label }}</span>
              <span class="bar-track">
                <span class="bar-fill" :style="{ width: `${bar.percent}%` }" />
              </span>
              <span class="bar-count mono">{{ bar.count }}</span>
            </li>
          </ul>
        </template>
      </section>

      <!-- 提问主题偏好 -->
      <section class="card">
        <div class="list-head">
          <h3>提问主题偏好</h3>
          <span class="muted tiny">
            共 {{ qSummary?.question_count ?? 0 }} 条提问 · {{ qSummary?.asker_count ?? 0 }} 人
          </span>
        </div>
        <ul v-if="topicBars.length" class="topic-list">
          <li
            v-for="topic in topicBars"
            :key="topic.key"
            @pointerenter="showTip($event, topic.label, [`${topic.count} 条提问 · 占 ${Math.round(topic.share * 100)}%`, ...topic.examples.slice(0, 3)])"
            @pointermove="moveTip"
            @pointerleave="hideTip"
          >
            <div class="topic-row">
              <span class="topic-dot" :style="{ background: topic.color }" />
              <span class="topic-name">{{ topic.label }}</span>
              <span class="mono topic-pct">{{ Math.round(topic.share * 100) }}%</span>
            </div>
            <span class="bar-track">
              <span class="bar-fill" :style="{ width: `${topic.percent}%`, background: topic.color }" />
            </span>
            <p v-if="topic.examples.length" class="topic-eg dim tiny">
              例：{{ topic.examples[0] }}
            </p>
          </li>
        </ul>
        <p v-else class="empty">{{ loading ? '读取中…' : '还没有提问记录。' }}</p>
      </section>
    </div>

    <!-- ============ 第二块：词云 ============ -->
    <section class="card">
      <div class="list-head">
        <h3>提问关键词词云</h3>
        <span class="muted tiny">
          {{ qSummary?.keyword_count ?? 0 }} 个有效词 · 只收出现 ≥ 2 次的词
        </span>
      </div>
      <div v-if="cloud.length" class="cloud-box">
        <!-- viewBox 必须和脚本里的 CLOUD_W/CLOUD_H 一致，否则碰撞算出来的坐标会被缩放错位 -->
        <svg viewBox="0 0 640 360" class="cloud" role="img" aria-label="提问关键词词云">
          <text
            v-for="word in cloud"
            :key="word.word"
            :x="word.x"
            :y="word.y"
            :font-size="word.size"
            :fill="word.color"
            :fill-opacity="word.opacity"
            text-anchor="middle"
            class="cloud-word"
            @pointerenter="showTip($event, word.word, [`出现 ${word.count} 次`, `权重 ${word.weight.toFixed(2)}`])"
            @pointermove="moveTip"
            @pointerleave="hideTip"
          >
            {{ word.word }}
          </text>
        </svg>
        <p class="dim tiny cloud-note">
          只出现一次的词汇字号小到看不清、悬停也只能回"出现 1 次"，属于噪声，已过滤。
          鼠标移到词上可看出现次数。
        </p>
      </div>
      <p v-else class="empty">
        {{ loading ? '读取中…' : '提问量还不够抽词。至少积累几条提问后再来看。' }}
      </p>
    </section>

    <!-- ============ 第三块：提问时段 ============ -->
    <div class="grid">
      <section class="card">
        <div class="list-head">
          <h3>一日内提问时段</h3>
          <span class="muted tiny">
            高峰 {{ qSummary?.peak_hour !== null && qSummary?.peak_hour !== undefined ? hourLabel(qSummary.peak_hour) : '—' }}
          </span>
        </div>
        <div v-if="hourBars.length" class="hour-chart">
          <div
            v-for="bar in hourBars"
            :key="bar.hour"
            class="hour-col"
            @pointerenter="showTip($event, hourLabel(bar.hour), [`${bar.count} 条提问`, bar.peak ? '高峰时段' : ''])"
            @pointermove="moveTip"
            @pointerleave="hideTip"
          >
            <span class="hour-bar" :class="{ peak: bar.peak }" :style="{ height: `${Math.max(2, bar.percent)}%` }" />
            <span v-if="bar.hour % 3 === 0" class="hour-tick">{{ bar.hour }}</span>
          </div>
        </div>
        <p v-else class="empty">{{ loading ? '读取中…' : '暂无提问时段数据。' }}</p>
        <p class="dim tiny">
          已按北京时间（UTC+8）分桶：库里存的是 UTC，直接分桶会把"晚上问得多"错判成"下午"。
        </p>
      </section>

      <section class="card">
        <div class="list-head">
          <h3>提问活跃度</h3>
          <span class="muted tiny">按星期 · 按人</span>
        </div>
        <div class="sub-title">星期分布</div>
        <ul class="bars">
          <li v-for="bucket in qa?.weekdays ?? []" :key="bucket.label">
            <span class="bar-label">{{ bucket.label }}</span>
            <span class="bar-track">
              <span
                class="bar-fill"
                :style="{ width: `${Math.round((bucket.count / Math.max(1, ...(qa?.weekdays ?? []).map((b) => b.count))) * 100)}%` }"
              />
            </span>
            <span class="bar-count mono">{{ bucket.count }}</span>
          </li>
        </ul>
        <div class="divider-line" />
        <div class="sub-title">提问最多的学生</div>
        <ul class="askers">
          <li v-for="(bucket, index) in qa?.top_askers ?? []" :key="bucket.label">
            <span class="rank">{{ index + 1 }}</span>
            <span class="asker-name">{{ bucket.label }}</span>
            <span class="mono dim tiny">{{ bucketLabel(bucket) }}</span>
          </li>
        </ul>
        <p v-if="!(qa?.top_askers ?? []).length" class="empty">暂无数据</p>
      </section>
    </div>

    <!-- ============ 第四块：画像逐条记录 ============ -->
    <section class="card">
      <div class="list-head">
        <h3>学生画像逐条记录</h3>
        <span class="muted tiny">
          最近 {{ portraitRows.length }} / {{ pSummary?.session_count ?? 0 }} 条
          <template v-if="pSummary?.truncated"> · 已达读取上限</template>
        </span>
      </div>
      <div v-if="portraitRows.length" class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>时间</th>
              <th>学生</th>
              <th>学号</th>
              <th>类型 / 年级</th>
              <th>绩点</th>
              <th>专业</th>
              <th>判定结果</th>
              <th>完整度</th>
              <th>针对政策</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in portraitRows" :key="row.session_id">
              <td class="mono tiny nowrap">{{ row.created_at ? formatTime(row.created_at) : '—' }}</td>
              <td class="nowrap">{{ row.student }}</td>
              <td class="mono tiny">{{ row.student_no }}</td>
              <td class="tiny nowrap">{{ row.student_type }} · {{ row.grade }}</td>
              <td class="mono tiny">{{ row.gpa }}</td>
              <td class="tiny ellipsis" :title="row.major">{{ row.major }}</td>
              <td class="nowrap">
                <span class="tag met">{{ row.met }}</span>
                <span class="tag not-met">{{ row.not_met }}</span>
                <span class="tag unknown">{{ row.unknown }}</span>
              </td>
              <td class="mono tiny">{{ Math.round(row.completeness * 100) }}%</td>
              <td class="tiny ellipsis" :title="row.document_title">{{ row.document_title }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">
        {{ loading ? '读取中…' : '还没有画像记录。' }}
      </p>
      <p class="dim tiny table-note">
        每个画像字段的原始写法另存了一份（`by_student_type` 已归并，`raw_student_types` 保留原值），
        用于核对归并是否误伤。
      </p>
    </section>

    <!-- 单一浮层：四张图共用 -->
    <Transition name="tip">
      <div v-if="tip" class="tip" :style="tipStyle">
        <strong>{{ tip.title }}</strong>
        <span v-for="(line, index) in tip.lines.filter(Boolean)" :key="index">{{ line }}</span>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.nowrap {
  white-space: nowrap;
}

.ellipsis {
  max-width: 190px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.table-wrap {
  overflow-x: auto;
}

.table-note {
  margin: var(--space-3) 0 0;
}

.sub-title {
  font-size: 12px;
  color: var(--c-text-2);
  margin-bottom: var(--space-2);
}

.divider-line {
  height: 1px;
  background: var(--c-border);
  margin: var(--space-3) 0;
}

/* ---------- 环形图 ---------- */
.donut-wrap {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.donut {
  width: 150px;
  height: 150px;
  flex: none;
}

.arc {
  cursor: pointer;
  transition: stroke-width 0.16s ease, opacity 0.16s ease;
}

.arc:hover {
  stroke-width: 20;
}

.donut-num {
  fill: var(--c-accent);
  font-size: 24px;
  font-weight: 700;
  text-anchor: middle;
  font-variant-numeric: tabular-nums;
}

.donut-cap {
  fill: var(--c-text-2);
  font-size: 10px;
  text-anchor: middle;
}

.legend {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  flex: 1;
}

.legend li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  padding: 2px 4px;
  border-radius: 6px;
  cursor: default;
}

.legend li:hover {
  background: var(--c-surface-2);
}

.legend i {
  width: 9px;
  height: 9px;
  border-radius: 3px;
  flex: none;
}

.legend b {
  margin-left: auto;
  color: var(--c-text);
  font-weight: 500;
}

/* ---------- 主题偏好 ---------- */
.topic-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 11px;
}

.topic-list > li {
  padding: 3px 4px;
  border-radius: 7px;
  cursor: default;
  transition: background 0.15s ease;
}

.topic-list > li:hover {
  background: var(--c-surface-2);
}

.topic-row {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 5px;
}

.topic-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: none;
}

.topic-name {
  font-size: 12.5px;
}

.topic-pct {
  margin-left: auto;
  font-size: 12px;
  color: var(--c-text-2);
}

.topic-eg {
  margin: 5px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 词云 ---------- */
.cloud {
  width: 100%;
  height: auto;
  display: block;
}

.cloud-word {
  cursor: default;
  font-family: inherit;
  font-weight: 600;
  transition: fill-opacity 0.15s ease;
}

.cloud-word:hover {
  fill-opacity: 1;
}

.cloud-note {
  margin: var(--space-2) 0 0;
  line-height: 1.7;
}

/* ---------- 时段 ---------- */
.hour-chart {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 140px;
  padding-bottom: 18px;
  position: relative;
}

.hour-col {
  flex: 1;
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: center;
  position: relative;
  cursor: default;
}

.hour-bar {
  width: 100%;
  max-width: 18px;
  border-radius: 4px 4px 2px 2px;
  background: var(--c-primary-weak);
  border: 1px solid var(--c-primary-border);
  transition: background 0.15s ease, border-color 0.15s ease;
}

.hour-bar.peak {
  background: linear-gradient(180deg, var(--c-primary) 0%, var(--c-primary-strong) 100%);
  border-color: var(--c-primary);
}

.hour-col:hover .hour-bar {
  background: var(--c-primary);
  border-color: var(--c-primary);
}

.hour-tick {
  position: absolute;
  bottom: -17px;
  font-size: 10px;
  color: var(--c-dim);
  font-variant-numeric: tabular-nums;
}

/* ---------- 活跃度 ---------- */
.askers {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.askers li {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 12.5px;
}

.rank {
  width: 18px;
  height: 18px;
  border-radius: 5px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10.5px;
  background: var(--c-surface-2);
  color: var(--c-text-2);
  flex: none;
}

.asker-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.askers .mono {
  margin-left: auto;
}

/* ---------- 浮层 ---------- */
.tip {
  position: fixed;
  z-index: 70;
  pointer-events: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-width: 240px;
  padding: 8px 11px;
  border-radius: 9px;
  background: rgba(14, 26, 24, 0.97);
  border: 1px solid var(--c-primary-border);
  box-shadow: var(--shadow-pop);
  font-size: 12px;
  color: var(--c-text);
  line-height: 1.55;
}

.tip strong {
  color: var(--c-primary);
  font-size: 12.5px;
}

.tip span {
  color: var(--c-text-2);
}

.tip-enter-active,
.tip-leave-active {
  transition: opacity 0.12s ease;
}

.tip-enter-from,
.tip-leave-to {
  opacity: 0;
}
</style>
