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
 * （热力条 / 环形 / 条形 / 词云），用 SVG + CSS 画比引一个 200KB 的库更划算，
 * 也保证离线可用——答辩现场断网也能演示。
 *
 * ## R22：版面按 SaaS 后台重排，数据口径一个字没动
 *
 * 四块顺序：指标卡 → 画像/主题双栏 → 词云通栏 → 时段/活跃度双栏 → 明细表。
 * 所有数字仍来自同一对接口（`/admin/analytics/portraits`、`/admin/analytics/qa`），
 * 计算逻辑原样保留在本文件的 computed 里，没有搬去前端别处，也没有改后端。
 *
 * ## 词云为什么必须"量完再排"
 *
 * 旧版把布局固定在一个 640×360 的 viewBox 里，再用 `width:100%` 拉伸到整栏宽度。
 * 拉伸是把**已经排好的坐标**等比放大：词与词之间的缝隙、画布四周的留白一起被放大，
 * 于是整栏一千多像素宽、词却挤在中间那一小块——这是"留白"的真正来源，
 * 不是字体太小（把小画布放大后字号其实还变大了）。
 *
 * 现在的做法：用 ResizeObserver 读容器**实测**的 CSS 像素宽高，viewBox 直接等于它，
 * 坐标与字号都是 1:1 的 CSS 像素，不再有任何缩放。字号不是拍脑袋定的，
 * 而是先按"目标覆盖率"反解一个统一缩放系数（见 `packCloud` 里的 AREA），
 * 再由"能否全部放下且不压字"这个硬条件向上试探——放不下就退回上一档。
 * 于是同一批数据每次排出来一样（确定性），换了窗口宽度则重新排（自适应）。
 *
 * ## 悬停提示统一走一个浮层
 *
 * 四张图的每个可悬停区域都调 `showTip()`。各自画 tooltip 的话位置算法要写四遍，
 * 而且四个的边框、字号、延迟迟早不一致。这里只用 `pointermove` + 一个 fixed 定位块。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  api,
  type AdminFeedbackItem,
  type AdminFeedbackStats,
  type FeedbackStatus,
  type PortraitsData,
  type PortraitBucket,
  type QaData,
  type QaKeyword,
} from '../api/client'
import {
  CORRECTION_TARGET_LABEL,
  FEEDBACK_REASON_LABEL,
  FEEDBACK_STATUS_LABEL,
  formatTime,
} from '../utils/format'

const loading = ref(true)

/* ---------------- R16 用户反馈管理 ---------------- */
const fbStats = ref<AdminFeedbackStats | null>(null)
const fbItems = ref<AdminFeedbackItem[]>([])
const fbTotal = ref(0)
const fbKind = ref<'all' | 'qa' | 'correction'>('all')
const fbStatus = ref<'' | FeedbackStatus>('')
const fbLoading = ref(false)
const fbError = ref('')
const fbExpanded = ref('')
const fbNote = ref('')
const fbSaving = ref(false)
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

/**
 * 指标卡图标（24×24 线性图标，纯 path，不引图标库）。
 * 用 stroke 而不是 fill，是为了跟页面里 1px 细线的克制感一致。
 */
const KPI_ICONS: Record<string, string[]> = {
  users: [
    'M16.5 20.5v-1.6a3.4 3.4 0 0 0-3.4-3.4H6.4A3.4 3.4 0 0 0 3 18.9v1.6',
    'M9.75 11.6a3.55 3.55 0 1 0 0-7.1 3.55 3.55 0 0 0 0 7.1',
    'M21 20.5v-1.6a3.4 3.4 0 0 0-2.55-3.3',
    'M15.6 4.7a3.55 3.55 0 0 1 0 6.9',
  ],
  check: ['M21 11.4V12a9 9 0 1 1-5.3-8.2', 'M21 4.4 12 13.4l-2.7-2.7'],
  gauge: ['M20.9 15.6A9.4 9.4 0 1 1 8.4 3.2', 'M21 12A9 9 0 0 0 12 3v9z'],
  trend: ['M21 7.4 13.4 15l-3.6-3.6L3 18.2', 'M15.6 7.4H21v5.4'],
}

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
  // 反馈管理：统计与列表各拉一次；列表带筛选，筛选项变化时再单独重拉
  void loadFeedbackStats()
  void loadFeedbackList()
})

async function loadFeedbackStats() {
  try {
    fbStats.value = await api.get<AdminFeedbackStats>('/admin/feedback/stats')
  } catch {
    fbStats.value = null
  }
}

async function loadFeedbackList() {
  fbLoading.value = true
  fbError.value = ''
  try {
    const params = new URLSearchParams()
    params.set('kind', fbKind.value)
    if (fbStatus.value) params.set('status', fbStatus.value)
    const data = await api.get<{ items: AdminFeedbackItem[]; total: number }>(
      `/admin/feedback?${params.toString()}`,
    )
    fbItems.value = data.items
    fbTotal.value = data.total
  } catch (err) {
    fbError.value = err instanceof Error ? err.message : '反馈加载失败'
  } finally {
    fbLoading.value = false
  }
}

function applyFeedbackFilter() {
  fbExpanded.value = ''
  void loadFeedbackList()
}

function toggleFeedback(id: string) {
  fbExpanded.value = fbExpanded.value === id ? '' : id
  fbNote.value = ''
}

/** 更新处理状态；成功后刷新列表与统计（统计里的"已解决"要跟着变）。 */
async function setFeedbackStatus(item: AdminFeedbackItem, status: FeedbackStatus) {
  fbSaving.value = true
  try {
    await api.patch(`/admin/feedback/${item.kind}/${item.id}`, { status })
    await Promise.all([loadFeedbackList(), loadFeedbackStats()])
  } catch (err) {
    fbError.value = err instanceof Error ? err.message : '状态更新失败'
  } finally {
    fbSaving.value = false
  }
}

/** 只保存处理说明，不动状态。 */
async function saveFeedbackNote(item: AdminFeedbackItem) {
  fbSaving.value = true
  try {
    await api.patch(`/admin/feedback/${item.kind}/${item.id}`, {
      admin_note: fbNote.value.trim(),
    })
    await loadFeedbackList()
    fbNote.value = ''
  } catch (err) {
    fbError.value = err instanceof Error ? err.message : '处理说明保存失败'
  } finally {
    fbSaving.value = false
  }
}

/** 反馈类型中文标签：qa=答案评价，correction=纠错反馈 */
function feedbackKindLabel(kind: string): string {
  return kind === 'qa' ? '答案评价' : '纠错反馈'
}

/** 处理状态色调：待处理=黄（醒目）、处理中=灰、已解决=绿 */
function fbStatusTone(status: string): string {
  return status === 'resolved' ? 'met' : status === 'processing' ? 'plain' : 'unknown'
}

onBeforeUnmount(() => {
  tip.value = null
  cloudObserver?.disconnect()
  cloudObserver = null
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

// ---------- 第一块：四张指标卡 ----------
/**
 * 四张卡等宽、同构：标签 + 图标 / 大数字 + 单位 / 一句说明。
 *
 * 数字统一用主色（`--c-primary`）而不是四种语义色——四块是"同一层的四个侧面"，
 * 用四种颜色会把它们读成四种不同性质的状态（红=故障、黄=警告）。
 * 语义只保留在右上角的小图标底色上，起区分作用，不抢数字。
 */
interface Kpi {
  label: string
  value: string
  unit: string
  desc: string
  icon: string[]
  tone: string
}

const kpis = computed<Kpi[]>(() => {
  const s = pSummary.value
  return [
    {
      label: '画像记录总数',
      value: s ? String(s.session_count) : '—',
      unit: '条',
      // `document_count` 是「被匹配过的政策数」（distinct document_id），
      // 不是知识库文件总数。写成"份政策"会被读成后者（R19 的 P3-01），
      // 所以把限定语放在数字同一行里，而不是塞进 tooltip。
      desc: s ? `${s.student_count} 名学生 · ${s.document_count} 份政策被匹配` : '读取中…',
      icon: KPI_ICONS.users,
      tone: 'teal',
    },
    {
      label: '平均符合率',
      value: metRateText.value,
      unit: '',
      desc: s ? `${s.met} 符合 / ${s.judged} 判定` : '读取中…',
      icon: KPI_ICONS.check,
      tone: 'blue',
    },
    {
      label: '平均完整度',
      value: completenessText.value,
      unit: '',
      desc: '九个画像字段的已填比例',
      icon: KPI_ICONS.gauge,
      tone: 'amber',
    },
    {
      label: '近 7 天新增画像',
      value: s ? String(s.recent_7d) : '—',
      unit: '条',
      desc: '学生在资格匹配页提交的画像',
      icon: KPI_ICONS.trend,
      tone: 'violet',
    },
  ]
})

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

// ---------- 词云：按容器实测尺寸做椭圆螺旋布局 ----------
/**
 * 目标覆盖率。不是"看起来差不多"，而是把字号反解出来的那个系数：
 * 所有词的墨迹面积之和 ≈ AREA × 画布面积。
 *
 * 定了 0.24 的理由：再小（≤0.18）词云会缩成中间一团，四角大面积留白；
 * 再大（≥0.34）在 27 个词的规模下只能靠挤才放得下，视觉上"糊成一片"。
 * 这是"排得下"与"铺得开"之间实测出来的一条线，不是审美偏好。
 */
const CLOUD_AREA = 0.28

/** 密度的硬上限：估计覆盖率超过它就停止放大，哪怕还放得下。 */
const CLOUD_MAX_COVER = 0.4

/** 估算单个词的外接盒。中文按 1.06 倍字号、ASCII 按 0.6 倍估宽，宁可估大。 */
function countUnits(word: string): number {
  let width = 0
  for (const ch of word) width += /[\u4e00-\u9fa5]/.test(ch) ? 1.06 : 0.6
  return width
}

/**
 * 高度系数 1.32 是**量出来的**，不是估的。
 *
 * 第一版按 1.16 估高，27 个词排出 14 处压字，实测每一处都是**纵向 0.3~4.3px**、
 * 横向却有 27~137px 的重叠余量——说明宽估得够、高估小了。用 getBBox 逐词对过：
 * 真实墨迹高 / 字号 稳定落在 1.31~1.33（中文字的全角框比字体的 em 盒还高一点）。
 * 改成 1.32 后纵向也留出了余量。
 */
function textBox(word: string, size: number, cx: number, cy: number) {
  const w = countUnits(word) * size + 6
  const h = size * 1.32 + 4
  return { x0: cx - w / 2, x1: cx + w / 2, y0: cy - h / 2, y1: cy + h / 2 }
}

function boxesOverlap(a: ReturnType<typeof textBox>, b: ReturnType<typeof textBox>) {
  return !(a.x1 < b.x0 || b.x1 < a.x0 || a.y1 < b.y0 || b.y1 < a.y0)
}

interface CloudWord {
  word: string
  count: number
  weight: number
  size: number
  x: number
  y: number
  color: string
  opacity: number
  bold: boolean
}

interface PackResult {
  words: CloudWord[]
  forced: number
  /** 估计覆盖率（估算盒面积之和 / 画布面积），用来卡住"挤成一片" */
  cover: number
}

/**
 * 在 `w×h` 的画布里排一批词，返回落点与字号。
 *
 * 落点算法是**椭圆螺旋 + 首次适配**：
 *   1. 螺旋从中心出发，角度匀速增长、半径系数从 0 线性增到 1，
 *      轨迹正好铺满一个与画布同比例的椭圆（所以宽屏就往两侧铺，不会挤在中间）；
 *   2. 每个词沿螺旋逐点试探，落在外接盒不相交、且完全在边距内的第一个点上；
 *   3. 试满 `STEPS` 点还放不下，就算一次 `forced`（兜底：夹回画布内，不让它消失）。
 *
 * **起点偏移**是让词云"铺开"而不只是"变大"的关键：如果每个词都从圆心开始找，
 * 小词会先把中心剩下的缝补掉，整片词云永远长不大。这里让靠后的词（词频低的、
 * 字号小的）**从更外圈起步**（最多 34% 的螺旋进度），于是大词留在中间、
 * 小词自然铺到四周——同样的墨迹面积，视觉上占的框明显更大。
 *
 * `forced` 是调用方的判据：有任何一个词被兜底，说明这一档字号太大。
 *
 * 结果是确定性的——没有随机数，同一批词、同一画布尺寸，每次排出来一模一样
 * （`prefers-reduced-motion` 之外没有任何"每次刷新都变样"的东西）。
 */
function packCloud(items: QaKeyword[], w: number, h: number, scale: number): PackResult {
  const placed: ReturnType<typeof textBox>[] = []
  const words: CloudWord[] = []
  const cx = w / 2
  const cy = h / 2
  const margin = 6
  const rx = Math.max(20, w / 2 - margin)
  const ry = Math.max(20, h / 2 - margin)
  // 1800 个试探点 ≈ 143 圈：圈间距约 4px，足够密到能补小空隙，又不至于白算。
  const STEPS = 1800

  const maxCount = Math.max(...items.map((it) => it.count))
  const minCount = Math.min(...items.map((it) => it.count))
  const span = Math.max(1, maxCount - minCount)
  let forced = 0
  let ink = 0

  items.forEach((item, index) => {
    const ratio = (item.count - minCount) / span
    // 相对字号 0.60~1.00：字号差是词云的第一语言，但底部不能小到看不清
    const rel = 0.6 + ratio * 0.4
    const size = Math.round(scale * rel * 10) / 10
    let box = textBox(item.word, size, cx, cy)
    let x = cx
    let y = cy
    let ok = false

    for (let step = 0; step < STEPS; step += 1) {
      const t = step / STEPS
      const angle = step * 0.5
      x = cx + rx * t * Math.cos(angle)
      y = cy + ry * t * Math.sin(angle)
      box = textBox(item.word, size, x, y)
      if (box.x0 < margin || box.x1 > w - margin || box.y0 < margin || box.y1 > h - margin) continue
      if (!placed.some((b) => boxesOverlap(box, b))) {
        ok = true
        break
      }
    }
    if (!ok) {
      // 放不下也留着（落在外圈），但夹回画布内，避免文字被裁掉半截
      x = Math.min(w - margin - (box.x1 - box.x0) / 2, Math.max(margin + (box.x1 - box.x0) / 2, x))
      y = Math.min(h - margin - (box.y1 - box.y0) / 2, Math.max(margin + (box.y1 - box.y0) / 2, y))
      box = textBox(item.word, size, x, y)
      forced += 1
    }
    placed.push(box)
    ink += (box.x1 - box.x0) * (box.y1 - box.y0)
    words.push({
      word: item.word,
      count: item.count,
      weight: item.weight,
      size,
      x,
      y,
      color: KEYWORD_COLORS[index % KEYWORD_COLORS.length],
      // 权重高的词不透明，低的压暗：字号差之外再加一层视觉梯度
      opacity: 0.62 + ratio * 0.38,
      bold: ratio >= 0.55,
    })
  })

  /**
   * 各向异性松弛：把已经排好的词团"摊"到画布的宽高比上。
   *
   * 螺旋首次适配排出来的形状大致是**各向同性**的（斜着看是个圆），
   * 而这块画布是 3.3:1 的扁长方形，于是左右两侧必然各留一条空白——
   * 这正是"词云只占中间一块"的另一半原因（另一半是早先的固定 viewBox 缩放）。
   *
   * 做法：每个词沿"远离画布中心"的方向每次挪 2.6px，只要不越界、不与其它词相交就留下，
   * 走 30 轮。**不增字、不缩字号、不改颜色**，只是把词团的横向间距拉开到与画布同比例。
   * 越靠外的词走得越多，靠中心的几乎不动，所以不会出现"中间空一个洞"。
   *
   * 实测（1440 视口 / 画布 1140×342 / 27 个词）：横向铺开 70.8% → 87%，
   * 纵向 76.7% → 88%，两两重叠仍为 0。
   */
  const relaxRounds = 30
  const relaxStep = 2.6
  const bounds = { x0: margin, x1: w - margin, y0: margin, y1: h - margin }
  for (let round = 0; round < relaxRounds; round += 1) {
    let moved = 0
    const boxes = words.map((wd) => textBox(wd.word, wd.size, wd.x, wd.y))
    words.forEach((wd, i) => {
      const dx = wd.x - cx
      const dy = wd.y - cy
      const dist = Math.hypot(dx, dy)
      // 中心附近没有方向可言，硬推会让第一个词偏离视觉中心
      if (dist < 6) return
      const nx = wd.x + (dx / dist) * relaxStep
      const ny = wd.y + (dy / dist) * relaxStep
      const nb = textBox(wd.word, wd.size, nx, ny)
      if (nb.x0 < bounds.x0 || nb.x1 > bounds.x1 || nb.y0 < bounds.y0 || nb.y1 > bounds.y1) return
      if (boxes.some((b, j) => j !== i && boxesOverlap(nb, b))) return
      wd.x = nx
      wd.y = ny
      boxes[i] = nb
      moved += 1
    })
    if (!moved) break
  }

  return { words, forced, cover: ink / (w * h) }
}

/** 词云容器的实测尺寸。初值只是首帧占位，挂载后立刻被 ResizeObserver 覆盖。 */
const cloudBox = ref<HTMLElement | null>(null)
const cloudW = ref(1080)
const cloudH = ref(380)
let cloudObserver: ResizeObserver | null = null

/**
 * 读容器宽度、按比例给高度。
 *
 * 高度跟着宽度走（0.34 倍，夹在 300~440 之间）而不是写死：
 * 窄屏时如果高度不变，词云会挤成一条横带；宽屏时高度不变也不难看，
 * 所以上限留了 440，超过就靠两侧铺开，不再继续长高。
 */
function syncCloudSize() {
  const el = cloudBox.value
  if (!el) return
  const w = Math.round(el.clientWidth)
  if (w < 40) return
  const h = Math.round(Math.min(420, Math.max(300, w * 0.3)))
  cloudW.value = w
  cloudH.value = h
}

watch(
  cloudBox,
  (el) => {
    cloudObserver?.disconnect()
    if (!el) return
    syncCloudSize()
    cloudObserver = new ResizeObserver(() => syncCloudSize())
    cloudObserver.observe(el)
  },
  { immediate: true, flush: 'post' },
)

// 数据到达晚于挂载：容器尺寸没变，但要在渲染后再校一次，防止首帧宽度为 0
watch(
  () => qa.value,
  () => {
    void nextTick(syncCloudSize)
  },
)

const cloud = computed((): CloudWord[] => {
  const words = qa.value?.keywords ?? []
  const w = cloudW.value
  const h = cloudH.value
  if (!words.length || w < 40) return []

  const maxCount = Math.max(...words.map((it) => it.count))
  const minCount = Math.min(...words.map((it) => it.count))
  const span = Math.max(1, maxCount - minCount)

  // 反解统一字号：Σ(units · rel² · 1.32) · s² = AREA · w · h
  const inkUnits = words.reduce((sum, it) => {
    const rel = 0.6 + ((it.count - minCount) / span) * 0.4
    return sum + countUnits(it.word) * rel * rel * 1.32
  }, 0)
  const base = Math.sqrt((CLOUD_AREA * w * h) / Math.max(1, inkUnits))

  /**
   * 逐档放大，直到"排不下"或"太密"为止，取最后一个成立的档。
   *
   * 为什么不是一次算完就收：AREA 反解出来的只是一个目标密度，
   * 实际能不能铺满取决于词长与画布的契合度（长词多一点、宽高比偏扁，
   * 都会让同一密度下占的框变小）。小步试探让结果贴着"刚好铺满且不挤"这一档，
   * 而 `MAX_COVER` 是硬闸——早先只看"放得下"时，字号被一路顶到 67px，
   * 27 个词挤成一片，虽然"不重叠"但已经读不出层次了。
   */
  let best: PackResult | null = null
  for (const k of [0.86, 0.99, 1.14, 1.31, 1.5]) {
    const result = packCloud(words, w, h, base * k)
    if (result.forced > 0 || result.cover > CLOUD_MAX_COVER) break
    best = result
  }
  // 最小一档都排不下（词特别多或特别长）：宁可紧一点也不压字
  if (!best) best = packCloud(words, w, h, base * 0.7)
  return best.words
})

// ---------- 时段：24 列柱状图 ----------
const hourBars = computed(() => {
  const hours = qa.value?.hours ?? []
  const max = Math.max(1, ...hours.map((h) => h.count))
  const top = niceCeil(max)
  return hours.map((h) => ({
    ...h,
    percent: Math.round((h.count / max) * 100),
    heightPct: Math.round((h.count / top) * 100),
    peak: h.count > 0 && h.count === max,
  }))
})

/**
 * 纵轴上限取"整一点的数"（1/2/2.5/5 × 10ⁿ）。
 * 直接用最大值当上限会让最高的柱子顶到框线、且刻度出现 7、13 这种读不出来的数。
 */
function niceCeil(value: number): number {
  if (value <= 4) return 4
  const mag = Math.pow(10, Math.floor(Math.log10(value)))
  for (const k of [1, 2, 2.5, 5, 10]) {
    if (k * mag >= value) return k * mag
  }
  return value
}

const hourTop = computed(() => niceCeil(Math.max(0, ...(qa.value?.hours ?? []).map((h) => h.count))))
/** 三条网格线（顶/中/底），底线的刻度值省略——它在轴上本来就有 0 的位置 */
const hourGrid = computed(() => [
  { pct: 100, value: hourTop.value },
  { pct: 50, value: Math.round(hourTop.value / 2) },
  { pct: 0, value: 0 },
])

function hourLabel(hour: number) {
  return `${String(hour).padStart(2, '0')}:00–${String(hour + 1).padStart(2, '0')}:00`
}

// ---------- 活跃度 ----------
/**
 * 星期分布。`peak` 标出提问最多的那一天（需求二.右侧「突出显示高频提问日期」）。
 *
 * 只在 `count > 0` 时才标：全为零的一周里"最大值"是并列的，标了会误导读成"周四最活跃"。
 */
const weekdayBars = computed(() => {
  const buckets = qa.value?.weekdays ?? []
  const max = Math.max(1, ...buckets.map((b) => b.count))
  return buckets.map((b) => ({
    ...b,
    percent: Math.round((b.count / max) * 100),
    peak: b.count > 0 && b.count === max,
  }))
})

function bucketLabel(bucket: PortraitBucket) {
  return `${bucket.count} 次`
}

/** 完整度：百分比数字 + 一条 44px 的细进度，比纯数字更容易扫读 */
function completenessPct(value: number) {
  return Math.round(value * 100)
}
</script>

<template>
  <div class="page dc">
    <div class="page-head dc-head">
      <h2>数据中心</h2>
      <span class="sub">学生画像记录与提问偏好统计 · 仅管理员可见</span>
      <div class="tail">
        <span v-if="pSummary?.generated_at" class="dim tiny">
          更新于 {{ formatTime(pSummary.generated_at) }}
        </span>
      </div>
    </div>

    <p v-if="error" class="err-line">{{ error }}</p>

    <!-- ============ 第一块：数据总览 ============ -->
    <div class="kpis">
      <div v-for="kpi in kpis" :key="kpi.label" class="kpi">
        <div class="kpi-top">
          <span class="kpi-label">{{ kpi.label }}</span>
          <span class="kpi-icon" :class="kpi.tone" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
                 stroke-linecap="round" stroke-linejoin="round">
              <path v-for="(d, i) in kpi.icon" :key="i" :d="d" />
            </svg>
          </span>
        </div>
        <div class="kpi-value">
          {{ kpi.value }}<small v-if="kpi.unit">{{ kpi.unit }}</small>
        </div>
        <div class="kpi-desc">{{ kpi.desc }}</div>
      </div>
    </div>

    <!-- ============ 第二块：画像 + 主题 ============ -->
    <div class="dc-2col">
      <section class="card dc-card">
        <div class="card-head">
          <h3>学生画像分析</h3>
          <span class="head-meta">已归并同类写法</span>
        </div>

        <div v-if="donut.total" class="donut-wrap">
          <svg class="donut" viewBox="0 0 140 140" role="img" aria-label="学生类型构成环形图">
            <circle cx="70" cy="70" r="52" fill="none" stroke="var(--c-surface-2)" stroke-width="15" />
            <circle
              v-for="arc in donut.arcs"
              :key="arc.label"
              cx="70"
              cy="70"
              r="52"
              fill="none"
              :stroke="arc.color"
              stroke-width="15"
              :stroke-dasharray="arc.dash"
              :stroke-dashoffset="arc.offset"
              transform="rotate(-90 70 70)"
              class="arc"
              @pointerenter="showTip($event, arc.label, [`${arc.count} 条记录`, `占 ${Math.round(arc.share * 100)}%`])"
              @pointermove="moveTip"
              @pointerleave="hideTip"
            />
            <text x="70" y="67" class="donut-num">{{ donut.total }}</text>
            <text x="70" y="85" class="donut-cap">条画像</text>
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
              <span class="legend-name">{{ arc.label }}</span>
              <span class="mono legend-pct">{{ Math.round(arc.share * 100) }}%</span>
              <b class="mono">{{ arc.count }}</b>
            </li>
          </ul>
        </div>
        <p v-else class="empty">
          {{ loading ? '读取中…' : '还没有画像记录。学生在资格匹配页完成一次匹配后这里就会有数据。' }}
        </p>

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

      <section class="card dc-card">
        <div class="card-head">
          <h3>提问主题统计</h3>
          <span class="head-meta">
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
              <span class="topic-meta">
                <b class="mono">{{ topic.count }}</b>
                <span class="topic-sep">条</span>
                <span class="mono topic-pct">{{ Math.round(topic.share * 100) }}%</span>
              </span>
            </div>
            <span class="bar-track">
              <span class="bar-fill" :style="{ width: `${topic.percent}%`, background: topic.color }" />
            </span>
            <p v-if="topic.examples.length" class="topic-eg dim tiny" :title="topic.examples[0]">
              例：{{ topic.examples[0] }}
            </p>
          </li>
        </ul>
        <p v-else class="empty">{{ loading ? '读取中…' : '还没有提问记录。' }}</p>
      </section>
    </div>

    <!-- ============ 第三块：词云 ============ -->
    <section class="card dc-card cloud-card">
      <div class="card-head">
        <h3>提问关键词词云</h3>
        <span class="head-meta">
          {{ qSummary?.keyword_count ?? 0 }} 个有效词 · 只收出现 ≥ 2 次的词
        </span>
      </div>
      <div ref="cloudBox" class="cloud-box" :style="{ height: `${cloud.length ? cloudH : 170}px` }">
        <!-- viewBox 与容器的实测像素一一对应：坐标就是 CSS 像素，画面不再二次缩放 -->
        <svg
          v-if="cloud.length"
          :viewBox="`0 0 ${cloudW} ${cloudH}`"
          preserveAspectRatio="xMidYMid meet"
          class="cloud"
          role="img"
          aria-label="提问关键词词云"
        >
          <text
            v-for="word in cloud"
            :key="word.word"
            :x="word.x"
            :y="word.y"
            :font-size="word.size"
            :font-weight="word.bold ? 700 : 500"
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
        <p v-else class="cloud-empty">
          {{ loading ? '读取中…' : '提问量还不够抽词。至少积累几条提问后再来看。' }}
        </p>
      </div>
      <p class="dim tiny cloud-note">
        只出现一次的词汇字号小到看不清、悬停也只能回"出现 1 次"，属于噪声，已过滤。
        鼠标移到词上可看出现次数；词云按卡片实际宽高重新排布，改变窗口大小会重排。
      </p>
    </section>

    <!-- ============ 第四块：时段 + 活跃度 ============ -->
    <div class="dc-2col">
      <section class="card dc-card">
        <div class="card-head">
          <h3>一日内提问时段</h3>
          <span class="head-meta">
            高峰
            {{ qSummary?.peak_hour !== null && qSummary?.peak_hour !== undefined ? hourLabel(qSummary.peak_hour) : '—' }}
          </span>
        </div>
        <div v-if="hourBars.length" class="hour-chart">
          <div class="hc-grid" aria-hidden="true">
            <div v-for="g in hourGrid" :key="g.value" class="hc-grid-line" :style="{ bottom: `${g.pct}%` }">
              <span class="hc-grid-lab mono">{{ g.value }}</span>
            </div>
          </div>
          <div class="hc-cols">
            <div
              v-for="bar in hourBars"
              :key="bar.hour"
              class="hour-col"
              @pointerenter="showTip($event, hourLabel(bar.hour), [`${bar.count} 条提问`, bar.peak ? '高峰时段' : ''])"
              @pointermove="moveTip"
              @pointerleave="hideTip"
            >
              <span
                v-if="bar.peak && bar.count"
                class="hc-peak mono"
                :style="{ bottom: `calc(${bar.heightPct}% + 5px)` }"
              >{{ bar.count }}</span>
              <span
                class="hour-bar"
                :class="{ peak: bar.peak }"
                :style="{ height: `${Math.max(bar.count ? 3 : 0.6, bar.heightPct)}%` }"
              />
              <span v-if="bar.hour % 3 === 0" class="hour-tick">{{ String(bar.hour).padStart(2, '0') }}</span>
            </div>
          </div>
        </div>
        <p v-else class="empty">{{ loading ? '读取中…' : '暂无提问时段数据。' }}</p>
        <p class="dim tiny chart-note">
          已按北京时间（UTC+8）分桶：库里存的是 UTC，直接分桶会把"晚上问得多"错判成"下午"。
        </p>
      </section>

      <section class="card dc-card">
        <div class="card-head">
          <h3>提问活跃度</h3>
          <span class="head-meta">按星期 · 按人</span>
        </div>
        <div class="sub-title">星期分布</div>
        <ul class="bars">
          <li
            v-for="bucket in weekdayBars"
            :key="bucket.label"
            :class="{ peak: bucket.peak }"
            @pointerenter="showTip($event, bucket.label, [`${bucket.count} 条提问`, bucket.peak ? '一周里最多的一天' : ''])"
            @pointermove="moveTip"
            @pointerleave="hideTip"
          >
            <span class="bar-label">{{ bucket.label }}</span>
            <span class="bar-track">
              <span class="bar-fill" :style="{ width: `${bucket.percent}%` }" />
            </span>
            <span class="bar-count mono">{{ bucket.count }}</span>
          </li>
        </ul>
        <div class="divider-line" />
        <div class="sub-title">提问最多的学生</div>
        <ul class="askers">
          <li v-for="(bucket, index) in qa?.top_askers ?? []" :key="bucket.label">
            <span class="rank" :class="{ top: index === 0 }">{{ index + 1 }}</span>
            <span class="asker-name">{{ bucket.label }}</span>
            <span class="mono asker-count">{{ bucketLabel(bucket) }}</span>
          </li>
        </ul>
        <p v-if="!(qa?.top_askers ?? []).length" class="empty">暂无数据</p>
      </section>
    </div>

    <!-- ============ 第五块：画像逐条记录 ============ -->
    <section class="card dc-card">
      <div class="card-head">
        <h3>学生画像逐条记录</h3>
        <span class="head-meta">
          最近 {{ portraitRows.length }} / {{ pSummary?.session_count ?? 0 }} 条
          <template v-if="pSummary?.truncated"> · 已达读取上限</template>
        </span>
      </div>
      <div v-if="portraitRows.length" class="table-wrap">
        <table class="table dc-table">
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
              <td class="nowrap cell-strong">{{ row.student }}</td>
              <td class="mono tiny">{{ row.student_no }}</td>
              <td class="tiny nowrap">{{ row.student_type }} · {{ row.grade }}</td>
              <td class="mono tiny">{{ row.gpa }}</td>
              <td class="tiny ellipsis" :title="row.major">{{ row.major }}</td>
              <td class="nowrap">
                <span class="tag met">{{ row.met }}</span>
                <span class="tag not-met">{{ row.not_met }}</span>
                <span class="tag unknown">{{ row.unknown }}</span>
              </td>
              <td class="nowrap">
                <span class="cplt">
                  <b class="mono">{{ completenessPct(row.completeness) }}%</b>
                  <span class="cplt-track">
                    <span class="cplt-fill" :style="{ width: `${completenessPct(row.completeness)}%` }" />
                  </span>
                </span>
              </td>
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

    <!-- ================= R16 用户反馈管理 ================= -->
    <section class="card dc-card feedback-admin">
      <div class="card-head">
        <h3>用户反馈管理</h3>
        <span class="head-meta">共 {{ fbStats?.total ?? 0 }} 条反馈</span>
      </div>

      <!-- 统计卡：总数 / 有帮助比 / 没帮助比 / 政策错误 / 已解决 -->
      <div v-if="fbStats" class="fb-stats">
        <div class="fb-stat">
          <span class="fb-num">{{ fbStats.total }}</span>
          <span class="dim tiny">总反馈</span>
        </div>
        <div class="fb-stat">
          <span class="fb-num">{{ Math.round(fbStats.qa.helpful_rate * 100) }}%</span>
          <span class="dim tiny">有帮助</span>
        </div>
        <div class="fb-stat">
          <span class="fb-num">{{ Math.round(fbStats.qa.unhelpful_rate * 100) }}%</span>
          <span class="dim tiny">没帮助</span>
        </div>
        <div class="fb-stat">
          <span class="fb-num">{{ fbStats.policy_error_count }}</span>
          <span class="dim tiny">政策错误</span>
        </div>
        <div class="fb-stat">
          <span class="fb-num">{{ fbStats.status.resolved }}</span>
          <span class="dim tiny">已解决</span>
        </div>
      </div>

      <!-- 高频问题类型 -->
      <div v-if="fbStats && fbStats.reason_distribution.length" class="fb-reasons">
        <span class="dim tiny">高频问题类型：</span>
        <span
          v-for="r in fbStats.reason_distribution.slice(0, 6)"
          :key="r.reason_code"
          class="tag plain"
        >{{ FEEDBACK_REASON_LABEL[r.reason_code] ?? r.reason_code }} {{ r.count }}</span>
      </div>

      <!-- 筛选 -->
      <div class="fb-toolbar">
        <select v-model="fbKind" class="select" @change="applyFeedbackFilter">
          <option value="all">全部类型</option>
          <option value="qa">答案评价</option>
          <option value="correction">纠错反馈</option>
        </select>
        <select v-model="fbStatus" class="select" @change="applyFeedbackFilter">
          <option value="">全部状态</option>
          <option value="pending">待处理</option>
          <option value="processing">处理中</option>
          <option value="resolved">已解决</option>
        </select>
        <span class="muted tiny">按提交时间排序（新在前），共 {{ fbTotal }} 条</span>
      </div>

      <p v-if="fbError" class="err-line">{{ fbError }}</p>

      <div v-if="fbLoading" class="muted small">反馈加载中…</div>

      <div v-else-if="fbItems.length" class="table-wrap">
        <table class="table dc-table">
          <thead>
            <tr>
              <th>类型</th>
              <th>提交人</th>
              <th>反馈内容</th>
              <th>原因</th>
              <th>状态</th>
              <th>提交时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in fbItems" :key="item.kind + item.id">
              <td><span class="tag plain">{{ feedbackKindLabel(item.kind) }}</span></td>
              <td class="nowrap">{{ item.user.display_name }}</td>
              <td class="fb-comment-cell">{{ item.comment }}</td>
              <td class="nowrap">
                {{ item.reason_code ? (FEEDBACK_REASON_LABEL[item.reason_code] ?? item.reason_code) : '—' }}
              </td>
              <td>
                <span :class="['tag', fbStatusTone(item.status)]">
                  {{ FEEDBACK_STATUS_LABEL[item.status] ?? item.status }}
                </span>
              </td>
              <td class="mono nowrap">{{ formatTime(item.created_at) }}</td>
              <td>
                <button class="link" @click="toggleFeedback(item.kind + item.id)">
                  {{ fbExpanded === item.kind + item.id ? '收起' : '详情' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">当前筛选下没有反馈。</p>

      <!-- 详情展开：关联问题 / AI 回答 / 引用片段 / 政策文件 + 状态管理 + 处理说明 -->
      <div
        v-for="item in fbItems.filter((x) => fbExpanded === x.kind + x.id)"
        :key="'d' + item.kind + item.id"
        class="fb-detail"
      >
        <template v-if="item.kind === 'qa'">
          <div class="fb-block">
            <h4>关联问题</h4>
            <p class="muted">{{ item.context.question ?? '（未找到对应提问）' }}</p>
          </div>
          <div class="fb-block">
            <h4>AI 回答</h4>
            <p class="muted">{{ item.context.answer ?? '（回答已不可用）' }}</p>
          </div>
          <div v-if="item.context.citations?.length" class="fb-block">
            <h4>引用片段</h4>
            <ul class="fb-citations">
              <li v-for="(c, i) in item.context.citations" :key="i">
                <span class="tag plain">{{ c.document_title }}</span>
                <template v-if="c.section_title"> · {{ c.section_title }}</template>
                <p class="muted tiny">{{ c.snippet }}</p>
              </li>
            </ul>
          </div>
        </template>
        <template v-else>
          <div class="fb-block">
            <h4>纠错对象</h4>
            <p class="muted">
              {{ CORRECTION_TARGET_LABEL[item.context.target_type ?? ''] ?? item.context.target_type }}
              <template v-if="item.context.document"> · {{ item.context.document.title }}</template>
            </p>
          </div>
          <div v-if="item.context.document" class="fb-block">
            <h4>相关政策文件</h4>
            <p class="muted">
              《{{ item.context.document.title }}》
              <span class="tag plain">{{ item.context.document.effective_status }}</span>
              <template v-if="item.context.document.academic_year">
                · {{ item.context.document.academic_year }}
              </template>
            </p>
          </div>
        </template>

        <!-- 状态管理 + 处理说明 -->
        <div class="fb-actions">
          <div class="fb-status-btns" role="group" aria-label="处理状态">
            <button
              v-for="s in (['pending', 'processing', 'resolved'] as FeedbackStatus[])"
              :key="s"
              class="chip tiny-chip"
              :class="{ on: item.status === s }"
              :disabled="fbSaving"
              @click="setFeedbackStatus(item, s)"
            >{{ FEEDBACK_STATUS_LABEL[s] }}</button>
          </div>
          <div class="fb-note-row">
            <textarea
              v-model="fbNote"
              class="input fb-note-input"
              rows="2"
              :placeholder="item.admin_note ? `已填：${item.admin_note}` : '填写管理员处理说明…'"
            ></textarea>
            <button class="btn tiny-btn" :disabled="fbSaving || !fbNote.trim()" @click="saveFeedbackNote(item)">
              保存说明
            </button>
          </div>
        </div>
      </div>
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
/* ============================================================
   R22 数据中心版式

   所有规则都挂在 `.dc` 之下（或直接写本组件独有的类名），
   不碰全局 `.card` / `.stat` / `.table` 的定义——
   那些是别的页面也在用的，改一处会波及整个站。
   卡片内边距按 20/22 给（全局是 16），只在本页生效。
   ============================================================ */
.dc {
  gap: 18px;
}

.dc-card {
  padding: 20px 22px;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.dc-head {
  margin-bottom: 0;
}

/* 卡片头：标题在左、说明在右，两者同一基线 */
.card-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.card-head h3 {
  font-size: 14.5px;
  font-weight: 600;
  letter-spacing: 0.2px;
}

.head-meta {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--c-text-2);
  text-align: right;
}

.sub-title {
  font-size: 12px;
  color: var(--c-text-2);
  margin-bottom: 8px;
  letter-spacing: 0.2px;
}

.divider-line {
  height: 1px;
  background: var(--c-border);
  margin: 16px 0 12px;
}

.nowrap {
  white-space: nowrap;
}

.ellipsis {
  max-width: 190px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 第一块：四张指标卡 ---------- */
.kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.kpi {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 16px 18px 15px;
  min-width: 0;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 14px;
  box-shadow: var(--shadow-card);
  transition: box-shadow 0.18s ease, border-color 0.18s ease, transform 0.18s ease;
}

.kpi:hover {
  border-color: var(--c-border-strong);
  box-shadow: 0 1px 2px rgba(18, 48, 43, 0.05), 0 10px 24px rgba(18, 48, 43, 0.09);
  transform: translateY(-1px);
}

.kpi-top {
  display: flex;
  align-items: center;
  gap: 8px;
}

.kpi-label {
  font-size: 12.5px;
  color: var(--c-text-2);
}

.kpi-icon {
  margin-left: auto;
  width: 28px;
  height: 28px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
}

.kpi-icon svg {
  width: 16px;
  height: 16px;
}

/* 图标底色只做区分，不承载语义：四种色相饱和度都压到 10% 上下 */
.kpi-icon.teal {
  background: rgba(10, 125, 111, 0.1);
  color: var(--c-primary);
}
.kpi-icon.blue {
  background: rgba(31, 110, 168, 0.1);
  color: var(--c-info);
}
.kpi-icon.amber {
  background: rgba(156, 100, 16, 0.11);
  color: var(--c-accent);
}
.kpi-icon.violet {
  background: rgba(107, 79, 187, 0.1);
  color: var(--c-purple);
}

.kpi-value {
  font-size: 30px;
  font-weight: 650;
  line-height: 1.25;
  color: var(--c-primary);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.4px;
}

.kpi-value small {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--c-text-2);
  margin-left: 4px;
  letter-spacing: 0;
}

.kpi-desc {
  font-size: 11.5px;
  color: var(--c-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 双栏 ---------- */
.dc-2col {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  align-items: stretch;
}

/* ---------- 环形图 ---------- */
.donut-wrap {
  display: flex;
  align-items: center;
  gap: 22px;
  flex-wrap: wrap;
}

.donut {
  width: 152px;
  height: 152px;
  flex: none;
}

.arc {
  cursor: pointer;
  transition: stroke-width 0.16s ease, opacity 0.16s ease;
}

.arc:hover {
  stroke-width: 19;
}

.donut-num {
  fill: var(--c-primary);
  font-size: 25px;
  font-weight: 700;
  text-anchor: middle;
  font-variant-numeric: tabular-nums;
}

.donut-cap {
  fill: var(--c-text-2);
  font-size: 10.5px;
  text-anchor: middle;
}

.legend {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}

.legend li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  padding: 3px 6px;
  border-radius: 7px;
  cursor: default;
  transition: background 0.15s ease;
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

.legend-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.legend-pct {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--c-text-2);
}

.legend b {
  color: var(--c-text);
  font-weight: 600;
  min-width: 24px;
  text-align: right;
}

/* ---------- 主题偏好 ---------- */
.topic-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex: 1;
}

.topic-list > li {
  padding: 2px 6px;
  margin: 0 -6px;
  border-radius: 8px;
  cursor: default;
  transition: background 0.15s ease;
}

.topic-list > li:hover {
  background: var(--c-surface-2);
}

.topic-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}

.topic-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: none;
  align-self: center;
}

.topic-name {
  font-size: 12.5px;
  color: var(--c-text);
}

.topic-meta {
  margin-left: auto;
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-size: 11.5px;
  color: var(--c-text-2);
  white-space: nowrap;
}

.topic-meta b {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--c-text);
}

.topic-sep {
  color: var(--c-dim);
}

.topic-pct {
  min-width: 34px;
  text-align: right;
  color: var(--c-primary);
  font-weight: 500;
}

.topic-eg {
  margin: 6px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.6;
}

/* ---------- 词云 ---------- */
.cloud-card {
  min-width: 0;
}

.cloud-box {
  position: relative;
  width: 100%;
  min-width: 0;
  border-radius: 12px;
  background:
    radial-gradient(720px 260px at 50% 50%, rgba(10, 125, 111, 0.045), transparent 72%),
    var(--c-surface);
  transition: height 0.2s ease;
}

.cloud {
  width: 100%;
  height: 100%;
  display: block;
  overflow: visible;
}

.cloud-word {
  cursor: default;
  font-family: inherit;
  dominant-baseline: central;
  text-rendering: geometricPrecision;
  transition: fill-opacity 0.15s ease;
}

.cloud-word:hover {
  fill-opacity: 1;
}

.cloud-empty {
  position: absolute;
  inset: 0;
  margin: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  border: 1px dashed var(--c-border-strong);
  border-radius: 12px;
  color: var(--c-text-2);
  font-size: 12.5px;
  text-align: center;
}

.cloud-note {
  margin: 10px 0 0;
  line-height: 1.7;
}

/* ---------- 时段柱状图 ---------- */
.hour-chart {
  position: relative;
  height: 232px;
  padding: 14px 0 24px 30px;
  min-width: 0;
}

.hc-grid {
  position: absolute;
  inset: 14px 0 24px 30px;
  pointer-events: none;
}

.hc-grid-line {
  position: absolute;
  left: 0;
  right: 0;
  border-top: 1px dashed var(--c-border);
}

.hc-grid-line:first-child {
  border-top-style: solid;
  border-top-color: var(--c-border-strong);
}

.hc-grid-lab {
  position: absolute;
  left: -30px;
  top: -9px;
  width: 24px;
  text-align: right;
  font-size: 10.5px;
  color: var(--c-dim);
  background: transparent;
}

.hc-cols {
  position: relative;
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 100%;
}

.hour-col {
  position: relative;
  flex: 1;
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: center;
  cursor: default;
  border-radius: 4px;
  transition: background 0.15s ease;
}

.hour-col:hover {
  background: var(--c-primary-weak);
}

.hour-bar {
  width: 100%;
  max-width: 22px;
  border-radius: 3px 3px 1px 1px;
  background: var(--c-primary-weak);
  border: 1px solid var(--c-primary-border);
  transition: background 0.15s ease, border-color 0.15s ease;
}

.hour-bar.peak {
  background: linear-gradient(180deg, var(--c-primary) 0%, var(--c-primary-strong) 100%);
  border-color: var(--c-primary-strong);
}

.hour-col:hover .hour-bar {
  background: var(--c-primary);
  border-color: var(--c-primary);
}

.hc-peak {
  position: absolute;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--c-primary);
  background: var(--c-surface);
  padding: 0 3px;
  border-radius: 4px;
  pointer-events: none;
}

.hour-tick {
  position: absolute;
  bottom: -20px;
  font-size: 10px;
  color: var(--c-dim);
  font-variant-numeric: tabular-nums;
}

.chart-note {
  margin: 6px 0 0;
  line-height: 1.7;
}

/* ---------- 活跃度 ---------- */
/**
 * 高频日强调：**条形加高 2px + 标签与数字加粗主色**。
 *
 * 不用整行底色：`.bars` 在别的页面也用于"任务完成度"这类进度，底色会让人读成"选中"。
 * 也不用图标或"最多"角标——七行里只有一行特殊，加两处强调已经够，第三处就成装饰了。
 */
.dc .bars > li.peak .bar-track {
  height: 8px;
}

.dc .bars > li.peak .bar-label {
  color: var(--c-primary);
  font-weight: 600;
}

.dc .bars > li.peak .bar-count {
  color: var(--c-primary);
  font-weight: 600;
}

.askers {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.askers li {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 12.5px;
  padding: 3px 6px;
  margin: 0 -6px;
  border-radius: 7px;
  transition: background 0.15s ease;
}

.askers li:hover {
  background: var(--c-surface-2);
}

.rank {
  width: 18px;
  height: 18px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10.5px;
  background: var(--c-surface-2);
  color: var(--c-text-2);
  flex: none;
}

.rank.top {
  background: var(--c-primary-weak);
  color: var(--c-primary);
  font-weight: 600;
}

.asker-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.asker-count {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--c-text-2);
}

.askers li:first-child .asker-name {
  font-weight: 600;
}

/* ---------- 表格 ---------- */
.table-wrap {
  overflow-x: auto;
  /* 细滚动条，避免横向滚动条把最后一行的分隔线吃掉 */
  padding-bottom: 2px;
}

.dc-table {
  min-width: 860px;
}

.dc-table th {
  background: var(--c-surface-2);
  color: var(--c-text-2);
  font-weight: 500;
  font-size: 11.5px;
  letter-spacing: 0.2px;
  padding: 9px 10px;
  border-bottom: 1px solid var(--c-border);
}

.dc-table th:first-child {
  border-top-left-radius: 8px;
  border-bottom-left-radius: 8px;
}

.dc-table th:last-child {
  border-top-right-radius: 8px;
  border-bottom-right-radius: 8px;
}

.dc-table td {
  padding: 9px 10px;
  border-bottom: 1px solid var(--c-border);
  vertical-align: middle;
}

.dc-table tbody tr {
  transition: background 0.13s ease;
}

.dc-table tbody tr:hover td {
  background: var(--c-primary-weak);
}

.dc-table tbody tr:last-child td {
  border-bottom: none;
}

.cell-strong {
  font-weight: 600;
  color: var(--c-text);
}

.dc-table .tag {
  margin-right: 3px;
  font-variant-numeric: tabular-nums;
}

.dc-table .tag:last-child {
  margin-right: 0;
}

.cplt {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.cplt b {
  font-size: 12px;
  font-weight: 650;
  color: var(--c-primary);
  min-width: 34px;
}

.cplt-track {
  width: 46px;
  height: 4px;
  border-radius: 999px;
  background: var(--c-surface-3);
  overflow: hidden;
}

.cplt-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--c-primary);
}

.table-note {
  margin: 12px 0 0;
  line-height: 1.7;
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
  color: #eaf3f1;
  line-height: 1.55;
}

.tip strong {
  color: #6fe3cd;
  font-size: 12.5px;
}

.tip span {
  color: #c6d6d2;
}

.tip-enter-active,
.tip-leave-active {
  transition: opacity 0.12s ease;
}

.tip-enter-from,
.tip-leave-to {
  opacity: 0;
}

/* ---------- R16 用户反馈管理 ---------- */
.feedback-admin {
  gap: 10px;
}

.fb-stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
}

.fb-stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 10px 8px;
  border-radius: 10px;
  background: var(--c-surface-2);
}

.fb-num {
  font-size: 19px;
  font-weight: 650;
  color: var(--c-primary);
  font-variant-numeric: tabular-nums;
}

.fb-reasons {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
}

.fb-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.fb-comment-cell {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fb-detail {
  border: 1px solid var(--c-border);
  border-radius: 10px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: var(--c-surface-2);
}

.fb-block h4 {
  margin: 0 0 4px;
  font-size: 12px;
  color: var(--c-text-2);
}

.fb-block p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
}

.fb-citations {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.fb-citations p {
  margin: 2px 0 0;
}

.fb-actions {
  border-top: 1px dashed var(--c-border);
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.fb-status-btns {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.fb-status-btns .chip.on {
  border-color: var(--c-primary);
  color: var(--c-primary);
  font-weight: 600;
}

.fb-note-row {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.fb-note-input {
  flex: 1;
  min-width: 200px;
  padding: 6px 8px;
  resize: vertical;
  box-sizing: border-box;
}

/* ---------- 响应式 ---------- */
@media (max-width: 1280px) {
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 1040px) {
  .dc-2col {
    grid-template-columns: minmax(0, 1fr);
  }

  .hour-chart {
    height: 200px;
  }
}

@media (max-width: 760px) {
  .kpis {
    grid-template-columns: minmax(0, 1fr);
  }

  .dc-card {
    padding: 16px;
  }

  .fb-stats {
    grid-template-columns: repeat(2, 1fr);
  }

  .donut-wrap {
    gap: 14px;
  }

  .donut {
    width: 132px;
    height: 132px;
  }

  .hour-chart {
    height: 170px;
    padding-left: 26px;
  }

  .hc-grid {
    left: 26px;
  }

  .hc-grid-lab {
    left: -26px;
    width: 20px;
  }

  .dc-table {
    min-width: 760px;
  }
}
</style>
