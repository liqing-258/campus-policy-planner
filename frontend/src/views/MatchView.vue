<script setup lang="ts">
/**
 * 资格匹配（M5）。
 *
 * 三种结果是三件事，不能合并成"符合/不符合"两态：
 * - 符合 / 不符合：字段齐全、算子可求值
 * - 无法判定：还要说清为什么（缺字段 / 条款主观 / 缺依据）
 *
 * 匹配完全不调模型（设计 §7.6）：同一个人填同样的画像，结果必须每次都一样，
 * 而且能追溯到政策原文的那一条。所以这一页不会出现"AI 判断"字样。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  ApiError,
  api,
  type DraftData,
  type DocumentRow,
  type DocumentVersions,
  type MatchData,
  type MatchItem,
  type MatchStatus,
  type Paged,
} from '../api/client'
import {
  MATCH_LABEL,
  MATCH_TONE,
  RULE_STATE,
  RULE_TYPE_LABEL,
  UNKNOWN_REASON_LABEL,
  evidenceLabel,
  formatDate,
} from '../utils/format'
import CorrectionModal from '../components/CorrectionModal.vue'
import { toConfirmPayload } from '../utils/plan'
import { GRADE_MAX_BY_TYPE, GRADE_MIN, useMatchStore } from '../stores/match'

const documents = ref<DocumentRow[]>([])
/** 每份文件"已复核的条件类规则"条数——匹配能不能跑，取决于这个数不是 0。 */
/**
 * 每份文件里出现过的奖项（target_group）。M6 补的缺口：
 * 同一份《奖学金评审办法》常同时写着励志奖学金（绩点 ≥ 3.0）与优秀学生奖学金（≥ 3.5），
 * 不选奖项时，绩点 3.2 的励志申请人会被优秀那条判成"不符合"——判错了人。
 * 这份清单来自已经在拉取的规则列表，不额外发请求。
 */
const groupsByDoc = computed<Record<string, string[]>>(() => {
  const out: Record<string, string[]> = {}
  for (const d of documents.value) out[d.id] = d.rule_summary?.target_groups ?? []
  return out
})
// R16 纠错反馈：匹配结果页上的上报入口（不改结论，只记反馈）
const correctionOpen = ref(false)
const matching = ref(false)
const error = ref('')
const notice = ref('')
/** 2008 时后端回传的"这份文件里真实的奖项清单"，用来让用户直接改选。 */
const noticeGroups = ref<string[]>([])

/**
 * **R8：画像与结论搬进 store。**
 *
 * 原来这四项是本组件的 ref，`<router-view>` 一切走组件就卸载——
 * 用户填满九项、跑出结论、点一下「智能问答」再点回来，全部归零。
 * 现在 documentId / targetGroup / profile / result 都在 `stores/match.ts`，
 * 落 sessionStorage（画像含绩点与家庭经济困难认定，不放 localStorage）。
 *
 * `profile` 取的是 store 里的**同一个响应式对象**（不是拷贝）：
 * 模板里 `profile[f.key] = ...` 直接改 store，省掉一层 watch 回写。
 * 前提是 store 的 `restore()` 用 `Object.assign` 原地改而不是整体替换。
 */
const match = useMatchStore()
// 从「校园事务指南」的「进行资格匹配」跳进来时，URL 上带着要匹配的文件
const route = useRoute()
/**
 * **必须在注册 `watch(documentId)` 之前同步恢复。**
 *
 * 恢复会把 `documentId` 从空串改成上次选的那份文件，而那个 watch 的职责是
 * "用户换了文件 → 清掉上一次的结论"。放在 `onMounted` 里恢复，这次变化会被
 * 当成"用户换了文件"，把刚恢复出来的 `result` 一起清掉——
 * 表现就是"刷新后画像还在、匹配结论没了"。同步恢复之后，watch 注册时
 * documentId 已经是最终值，不会误触发。
 */
match.restore()

const documentId = computed(() => match.documentId)
const targetGroup = computed(() => match.targetGroup)
const result = computed(() => match.result)
const confirmedId = computed(() => match.confirmedId)
const profile = match.profile

const draft = ref<DraftData | null>(null)
const drafting = ref(false)
const confirming = ref(false)

type Field = {
  key: string
  label: string
  hint: string
  type: string
  /** 数值字段的取值范围：用于越界校验，也写进提示文字 */
  min?: number
  max?: number
  /** 只接受整数（年级）：小数点在输入时就被抹掉 */
  integer?: boolean
  /** 枚举字段用下拉，避免用户照着英文枚举值猜怎么写 */
  options?: { value: string; label: string }[]
  /** R18：画像分组——按业务含义归类，表单不再平铺九格 */
  group?: string
}

/**
 * 画像字段。
 *
 * 早先这里把后端枚举值直接写在 placeholder 里（`full_time_undergraduate` / `yes / no`），
 * 用户得猜"我该填什么才算数"。改成下拉 + 中文提示：选的是中文，提交的是后端认的值。
 * 留空仍然是"缺少信息"，不是"不符合"。
 *
 * 数值字段全部带 min/max：`type="number"` 只挡住键盘上的字母，
 * 挡不住负号和 `1e9`——年级填 -4、绩点填 -1 都会原样进请求，
 * 后端白名单只校验字段名不校验取值，最后变成一个荒谬的匹配结论。
 * 所以数值框改成 text + inputmode，字符在输入时就过滤（见 onNumberInput）。
 */
const fields: Field[] = [
  {
    key: 'student_type',
    label: '学生类型',
    hint: '',
    type: 'select',
    group: 'enrollment',
    options: [
      { value: '全日制在校本科生', label: '全日制在校本科生' },
      { value: '全日制在校专科生', label: '全日制在校专科生' },
      { value: '全日制在校硕士研究生', label: '全日制在校硕士研究生' },
      { value: '全日制在校博士研究生', label: '全日制在校博士研究生' },
      { value: '非全日制研究生', label: '非全日制研究生' },
      { value: '交换生', label: '交换生' },
      { value: '旁听生', label: '旁听生' },
    ],
  },
  {
    key: 'grade',
    label: '年级',
    // hint 在模板里由 hintOf() 动态拼：上限随学生类型走（专科 1–3 / 本科 1–4 / 其余 1–8）
    hint: '',
    type: 'number',
    group: 'enrollment',
    // 年级只有整数：`2.5 年级`没有任何含义，小数点在输入时就被抹掉
    integer: true,
    min: GRADE_MIN,
    // 不写 max：它是动态的，见 boundsOf()
  },
  { key: 'gpa', label: '平均学分绩点', hint: '0–5，如 3.2', type: 'number', group: 'academic', min: 0, max: 5 },
  {
    key: 'rank_percent',
    label: '专业排名百分比',
    hint: '0–100，越小越靠前',
    type: 'number',
    group: 'ranking',
    min: 0,
    max: 100,
  },
  {
    key: 'poverty_certified',
    label: '家庭经济困难认定',
    hint: '',
    type: 'select',
    group: 'other',
    options: [
      { value: 'yes', label: '已认定' },
      { value: 'no', label: '未认定' },
    ],
  },
  {
    key: 'disciplinary_record',
    label: '纪律处分记录',
    hint: '',
    type: 'select',
    group: 'other',
    options: [
      { value: 'none', label: '无' },
      { value: 'any', label: '有' },
    ],
  },
  { key: 'major', label: '专业', hint: '如：物联网工程', type: 'text', group: 'other' },
  {
    key: 'credits_earned',
    label: '已修学分',
    hint: '0–300，如 120',
    type: 'number',
    group: 'academic',
    min: 0,
    max: 300,
  },
  {
    key: 'enrollment_status',
    label: '学籍状态',
    hint: '',
    type: 'select',
    group: 'enrollment',
    options: [
      { value: 'enrolled', label: '在籍在读' },
      { value: 'suspended', label: '休学' },
      { value: 'graduated', label: '已毕业' },
    ],
  },
]

/** R18：画像分组元数据——四组按业务含义归类，顺序即展示顺序。 */
const FIELD_GROUPS = [
  { id: 'enrollment', label: '学籍基本信息' },
  { id: 'academic', label: '学业成绩信息' },
  { id: 'ranking', label: '专业排名信息' },
  { id: 'other', label: '其他资格信息' },
]

/** 每个分组下的字段，保持 fields 里的原始顺序；空分组不渲染。 */
const fieldsByGroup = computed(() =>
  FIELD_GROUPS.map((g) => ({
    ...g,
    fields: fields.filter((f) => f.group === g.id),
  })).filter((g) => g.fields.length > 0),
)

/** 已填写字段数（非空字符串才算填了），用于"还差哪些"的进度提示。 */
const filledCount = computed(() =>
  fields.filter((f) => String(profile[f.key] ?? '').trim() !== '').length,
)

async function loadDocuments() {
  try {
    // R21：`match_ready=1` 让后端只返回"至少有一条已复核资格条件"的文件。
    // 不在前端过滤，是因为"能不能匹配"的判据在后端（approved + 条件类），
    // 前端再实现一遍就有两份真相；而且分页总数也得按过滤后的集合算。
    const data = await api.get<Paged<DocumentRow>>(
      '/documents?page=1&page_size=50&parse_status=indexed&match_ready=1',
    )
    // R20：每份文件的准备度随列表一起回来（`rule_summary`），不再逐个文件查规则。
    documents.value = [...data.items].sort(
      (a, b) =>
        (b.rule_summary?.approved_conditions ?? 0) - (a.rule_summary?.approved_conditions ?? 0),
    )
    // 选中的文件必须仍在列表里：管理员撤掉某份政策的复核后，本地存着的 documentId
    // 就成了"指向一个不该出现的选项"，直接用它请求会吃一个 2008。
    const stillValid = documents.value.some((d) => d.id === match.documentId)
    if (!stillValid) {
      match.documentId = documents.value[0]?.id ?? ''
      match.persist()
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '文档加载失败'
  }
}

/**
 * 字段的**实际**边界。年级的上限不是常量，它由学生类型决定
 * （专科 1–3、本科 1–4、其余 1–8），所以不能像其他字段那样写在 `fields` 里。
 */
function boundsOf(f: Field): { min?: number; max?: number } {
  if (f.key === 'grade') return { min: GRADE_MIN, max: match.gradeMax }
  return { min: f.min, max: f.max }
}

/** 年级输入框的提示：把当前上限说出来，用户才不用猜为什么 5 被改成 4 */
function hintOf(f: Field): string {
  if (f.key === 'grade') return `${GRADE_MIN}–${match.gradeMax}，可选可输`
  return f.hint
}

/**
 * 读画像字段，永远拿到字符串。
 *
 * 这里必须兜一层 `String()`：数值框若用 `v-model` + `type="number"`，
 * Vue 会把输入转成 number 写回，于是 `.trim()` / `.replace()` 直接 TypeError，
 * 整个页面渲染树跟着崩掉——粘贴、密码管理器自动填充、脚本赋值都会踩中。
 * 现在模板已改成 `:value` + 手写 `@input`（值始终是字符串），这个兜底留着防回归。
 */
function textOf(key: string): string {
  const v = profile[key] as string | number | null | undefined
  return typeof v === 'string' ? v : String(v ?? '')
}

/**
 * 数值框只接受数字与一个小数点：负号、字母、空格、第二个小数点在敲进来时就被抹掉。
 *
 * 为什么不交给 `type="number"`：它只挡字母，`-4`、`1e9`、粘贴值都照进；
 * 而它的上下箭头是系统浅色控件，深色主题里既刺眼又没用（年级 1–8 键盘更快），
 * 只能整个隐藏——那还不如一开始就用 text + inputmode，自己管字符。
 *
 * **`integer` 字段（年级）连小数点也不留**：`2.5 年级`没有含义，
 * 而 `+ / -` 这类符号在上面这条正则里本来就已经被抹掉了。
 */
function onNumberInput(f: Field, event: Event) {
  const el = event.target as HTMLInputElement
  const raw = el.value
  let next = raw.replace(f.integer ? /[^\d]/g : /[^\d.]/g, '')
  if (!f.integer) {
    const dot = next.indexOf('.')
    if (dot >= 0) {
      next = next.slice(0, dot + 1) + next.slice(dot + 1).replace(/\./g, '')
    }
  }
  // 只在真的改了字符时才回写 DOM：否则会把光标弹到末尾，
  // 在中间插一位数字时光标乱跳。
  if (next !== raw) el.value = next
  profile[f.key] = next
}

/**
 * 失焦时统一"写法"，并按字段性质决定越界值怎么处理。
 *
 * **两类字段两种策略，故意的**：
 * - **年级夹取到边界**。它的合法域由学生类型决定（专科没有第 5 年），
 *   输 5 不是"可能算错了"，而是"这个值不存在"，夹到 3 是纠正非法输入。
 *   用户的要求也是这一条：超过 3 固定 3，低于 1 固定 1。
 * - **其余数值字段不夹取**。绩点填 9 有可能真的算错了，
 *   悄悄改成 5 等于替用户编了一个成绩，他还以为自己填的就是 5。
 *   这类越界原样留着，由下面那行红字指出来。
 */
function normalizeNumber(f: Field) {
  const raw = textOf(f.key).trim()
  if (!raw) return
  const value = Number(raw)
  if (!Number.isFinite(value)) return
  if (f.key === 'grade') {
    // 夹取并取整：`2.9` 是输入过程中的中间态，落定后按最近的整数算
    const rounded = Math.round(value)
    profile.grade = String(Math.min(Math.max(rounded, GRADE_MIN), match.gradeMax))
    return
  }
  if (f.min !== undefined && value < f.min) return
  if (f.max !== undefined && value > f.max) return
  profile[f.key] = String(value)
}

/**
 * 换学生类型后年级可能越界（本科 4 年级 → 改成专科，上限只剩 3）。
 * 这里立刻夹回去，而不是留一个红字等用户自己发现——
 * 用户改的是"我是什么类型"，那就该由系统把随之失效的年级一起修正。
 */
watch(() => profile.student_type, () => {
  if (!profile.grade.trim()) return
  const value = Number(profile.grade)
  if (!Number.isFinite(value)) {
    profile.grade = ''
    return
  }
  const bounded = Math.min(Math.max(Math.round(value), GRADE_MIN), match.gradeMax)
  if (String(bounded) !== profile.grade) profile.grade = String(bounded)
})

/** 越界或非法的字段：列出来并禁用提交，而不是静默纠正成边界值 */
const invalidFields = computed(() =>
  fields
    .filter((f) => {
      if (f.type !== 'number') return false
      const raw = textOf(f.key).trim()
      if (!raw) return false
      const value = Number(raw)
      if (!Number.isFinite(value)) return true
      const { min, max } = boundsOf(f)
      if (min !== undefined && value < min) return true
      if (max !== undefined && value > max) return true
      return false
    })
    .map((f) => f.label),
)

function buildProfile(): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const f of fields) {
    const raw = textOf(f.key).trim()
    if (!raw) continue
    if (f.type === 'number') {
      const value = Number(raw)
      // 模板层已经挡了常见输入，这里再兜一次：粘贴、自动填充都能绕过模板
      if (!Number.isFinite(value)) continue
      const { min, max } = boundsOf(f)
      if (min !== undefined && value < min) continue
      if (max !== undefined && value > max) continue
      out[f.key] = value
    } else {
      out[f.key] = raw
    }
  }
  return out
}

async function runMatch() {
  if (!match.documentId) return
  // 点按钮会先触发输入框 blur（夹取已生效），这里再夹一次兜底：
  // 键盘直接按 Enter 提交、或脚本改值都不经过 blur
  if (profile.grade.trim()) normalizeNumber(fields.find((f) => f.key === 'grade')!)
  matching.value = true
  error.value = ''
  notice.value = ''
  noticeGroups.value = []
  match.setResult(null)
  draft.value = null
  try {
    const body: Record<string, unknown> = {
      document_id: match.documentId,
      profile: buildProfile(),
    }
    // 不选奖项就不发这个字段：后端会把文件里所有条件一起比，这是 M5 的行为，
    // 也是"我只是想看看这份办法写了哪些条件"的合理用法。
    if (match.targetGroup) body.target_group = match.targetGroup
    const envelope = await api.raw<MatchData>('/matches', body)
    if (envelope.code === 0) {
      match.setResult(envelope.data as MatchData)
      // 结论变化后默认把三类都显示出来：用户刚跑完匹配，想看的是全貌
      shownStatuses.value = [...ALL_STATUSES]
    } else if (envelope.code === 2008) {
      notice.value = envelope.message
      const payload = envelope.data as { available_groups?: string[] } | null
      noticeGroups.value = payload?.available_groups ?? []
    } else {
      error.value = `${envelope.code} · ${envelope.message}`
    }
  } catch (err) {
    error.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '匹配失败')
  } finally {
    matching.value = false
    match.persist()
  }
}

async function makeDraft() {
  if (!match.result) return
  drafting.value = true
  error.value = ''
  try {
    draft.value = await api.post<DraftData>('/plans/draft', {
      document_id: match.result.document_id,
      match_session_id: match.result.session_id,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : '生成计划失败'
  } finally {
    drafting.value = false
    match.persist()
  }
}

async function confirmDraft() {
  if (!draft.value) return
  confirming.value = true
  error.value = ''
  try {
    // 映射只在 utils/plan.ts 里写一份：R13 的推荐卡片也要"生成办理计划"，
    // 两边各抄一遍，早晚一边记得回传 source_rule_code、另一边忘了
    const plan = await api.post<{ plan_id: string; task_count: number }>(
      `/plans/${draft.value.plan_id}/confirm`,
      toConfirmPayload(draft.value),
    )
    match.confirmedId = plan.plan_id
    draft.value = null
  } catch (err) {
    error.value = err instanceof Error ? err.message : '确认失败'
  } finally {
    confirming.value = false
    match.persist()
  }
}

// ---------- 结果筛选（R8） ----------
/**
 * 三类结论的筛选。
 *
 * 用户的原话是"筛选不符合、符合、无法判定这三类中的任意一类或多类"，
 * 所以它是**多选**而不是单选：`[]` 表示一类都不看（结果为空的提示由模板兜），
 * 全选等于不筛。初始为全选，跑完一次匹配也重置为全选——
 * 用户刚点完「开始匹配」，想看的是全貌，不是上次剩下的一半。
 *
 * 筛选只影响渲染，不影响 `result.summary` 的三个计数：
 * 计数是"这次匹配的事实"，筛掉某一类不该让它变成 0。
 */
const ALL_STATUSES: MatchStatus[] = ['not_met', 'met', 'unknown']
const shownStatuses = ref<MatchStatus[]>([...ALL_STATUSES])

function toggleStatus(status: MatchStatus) {
  const list = shownStatuses.value
  shownStatuses.value = list.includes(status)
    ? list.filter((s) => s !== status)
    : ALL_STATUSES.filter((s) => s === status || list.includes(s))
}

/** 被筛掉的条目总数——不显示条目本身，但要让用户知道"这里还有东西" */
const hiddenTotal = computed(() => {
  if (!match.result) return 0
  return match.result.items.filter((i) => !shownStatuses.value.includes(i.status)).length
})

const grouped = computed(() => {
  if (!match.result) return []
  // 顺序按"不符合 → 无法判定 → 符合"：需要行动（改材料、补信息）的排在前面
  const order: MatchStatus[] = ['not_met', 'unknown', 'met']
  return order
    .filter((status) => shownStatuses.value.includes(status))
    .map((status) => ({
      status,
      items: match.result!.items.filter((i) => i.status === status),
    }))
    .filter((g) => g.items.length > 0)
})

const currentDoc = computed(() => documents.value.find((d) => d.id === match.documentId))
/** 当前文件的准备度；空对象表示文档还没加载完。 */
const currentSummary = computed(() => currentDoc.value?.rule_summary ?? null)
/** 只有「已具备匹配条件」才让按钮可点；其余状态展示的是原因，不是报错。 */
const canMatch = computed(() => currentSummary.value?.state === 'ready')

/* R15：选中的文件若存在多个版本，开跑前提醒核对（结果里也会带 version_note）。
   提醒不替用户选版本——哪版有效只有管理员的标注说了算。 */
const selectVersions = ref<DocumentVersions | null>(null)
watch(documentId, async (id) => {
  selectVersions.value = null
  if (!id) return
  try {
    selectVersions.value = await api.get<DocumentVersions>(`/documents/${id}/versions`)
  } catch {
    selectVersions.value = null
  }
}, { immediate: true })
const groups = computed(() =>
  match.documentId ? groupsByDoc.value[match.documentId] ?? [] : [],
)

/**
 * 挂载时只拉文件列表——会话恢复已经在 setup 顶部同步做完了（见那里的注释）。
 *
 * `loadDocuments()` 里有 `if (!documentId) 就默认选一份`：
 * 必须等恢复先发生，否则用户上次选的文件会被覆盖成"条件最多的那一份"，
 * 画像却还是上一份的，两者错位。
 */
onMounted(async () => {
  await loadDocuments()

  // 指南跳过来的 ?document_id= 在列表加载完之后再套用：
  // 只有那份文件确实在"可匹配列表"里才覆盖默认选择，否则下拉框会显示成空白
  // （select 的 value 指向一个不存在的 option）。
  const fromQuery = typeof route.query.document_id === 'string' ? route.query.document_id : ''
  if (fromQuery && documents.value.some((doc) => doc.id === fromQuery)) {
    match.documentId = fromQuery
    match.persist()
  }

  // R13：推荐卡片的「进行资格匹配」还会带奖项（`?target_group=`）。
  // **必须等上面那次 documentId 变更触发的 watch 跑完再设**——那个 watch 的职责是
  // "换了文件就清掉奖项"，同步写会在下一拍被它当成"用户换了文件"清掉，
  // 表现就是"从推荐点进去，奖项没选上"。
  const group = typeof route.query.target_group === 'string' ? route.query.target_group : ''
  if (group) {
    await nextTick()
    // 只接受这份文件里真实存在的奖项：否则下拉框会显示成空白（value 指向不存在的 option）
    if ((groupsByDoc.value[match.documentId] ?? []).includes(group)) {
      match.targetGroup = group
      match.persist()
    }
  }
})

// 换文件必须清空奖项：上一份文件里的「励志奖学金」在这份文件里可能根本不存在，
// 留着会让下一次请求直接吃一个 2008，用户还得自己猜是哪里出了问题。
watch(documentId, () => {
  match.targetGroup = ''
  match.setResult(null)
  draft.value = null
  notice.value = ''
  noticeGroups.value = []
  error.value = ''
  match.persist()
})

/**
 * 画像与选择立刻落盘。
 *
 * 没有防抖，理由是这一页的数据量：画像几百字节，一次匹配结论几 KB，
 * 没有 `stores/qa.ts` 那种"每追加一条就 stringify 整个会话"的放大效应。
 * 而防抖会留一个窗口期，正好覆盖"用户输完最后一个字、立刻点侧栏切页"——
 * 那正是这个缺陷被报上来的场景。
 */
watch(profile, () => match.persist(), { deep: true })
watch(
  () => [match.documentId, match.targetGroup, match.confirmedId],
  () => match.persist(),
)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>资格匹配</h2>
        <p class="muted small">
          按政策条款逐条比对，结果只有三种：符合、不符合、无法判定。
          「无法判定」会说明缺什么——这是系统主动承认不知道，而不是猜一个结论。
        </p>
      </div>
    </header>

    <section class="card">
      <!-- R21：列表只含"已有一条以上已复核资格条件"的政策，所以这里的选项文案
           回到三要素：名称 / 已复核条件数 / 片段数。状态标签不再重复——
           能出现在这里本身就是"已具备匹配条件"。 -->
      <div v-if="!documents.length && !error" class="match-empty">
        <span class="match-empty-icon" aria-hidden="true">◍</span>
        <p class="match-empty-title">当前没有可用于资格匹配的政策</p>
        <p class="muted small">
          资格匹配只列出「至少有一条已复核资格条件」的政策。目前管理员还没有完成任何一份的
          条件抽取与复核，所以这里为空——这不是故障。
        </p>
        <p class="muted tiny">
          可以先去「校园事务指南」看办理方式，或到「政策知识库」查阅原文；
          管理员在「规则管理」里复核通过条件后，那份政策会自动出现在这里。
        </p>
      </div>
      <div v-else class="row">
        <label class="field grow">
          <span>政策文件</span>
          <select v-model="match.documentId" class="select">
            <option value="" disabled>选择一份可用于匹配的政策</option>
            <option v-for="d in documents" :key="d.id" :value="d.id">
              {{ d.title }}（已复核条件 {{ d.rule_summary?.approved_conditions ?? 0 }} 条 ·
              {{ d.chunk_count }} 片段）
            </option>
          </select>
        </label>
        <label v-if="groups.length" class="field award">
          <span>申请奖项</span>
          <select v-model="match.targetGroup" class="select">
            <option value="">不区分奖项（比对全部条件）</option>
            <option v-for="g in groups" :key="g" :value="g">{{ g }}</option>
          </select>
        </label>
      </div>
      <p v-if="groups.length" class="muted tiny">
        这份文件里有 {{ groups.length }} 套奖项条件。不选奖项会把全部条件一起比对——
        绩点 3.2 在「励志奖学金」下符合、在「优秀学生奖学金」下不符合，混着比会判错人。
        选中奖项后，只比对明确标注该奖项的条件，以及未标注奖项的通用条件。
      </p>
      <!-- R20：选中文件后先给结论，再让用户填表。
           之前这里的判断只有"有 0 条已复核条件"一种说法，而 0 条背后有五种处境；
           现在按状态说清是哪一种，以及下一步该谁做什么。 -->
      <div v-if="currentDoc && currentSummary" class="rule-state" :class="`tone-${RULE_STATE[currentSummary.state]?.tone ?? 'plain'}`">
        <div class="rule-state-head">
          <span :class="['tag', RULE_STATE[currentSummary.state]?.tone ?? 'plain']">
            {{ RULE_STATE[currentSummary.state]?.label ?? currentSummary.state }}
          </span>
          <span class="muted tiny">
            已复核条件 {{ currentSummary.approved_conditions }} 条
            <template v-if="currentSummary.total_rules"> · 共 {{ currentSummary.total_rules }} 条规则</template>
            <template v-if="currentSummary.pending_review"> · 待复核 {{ currentSummary.pending_review }} 条</template>
          </span>
        </div>
        <p class="rule-state-hint">{{ RULE_STATE[currentSummary.state]?.hint }}</p>
        <p v-if="currentSummary.state === 'not_applicable' && currentSummary.applicability_note" class="muted tiny">
          判定理由：{{ currentSummary.applicability_note }}
        </p>
        <p v-if="currentSummary.state === 'extract_failed' && currentSummary.last_attempt_message" class="err-line tiny">
          {{ currentSummary.last_attempt_message }}
        </p>
        <p v-if="currentSummary.state === 'no_conditions'" class="muted tiny">
          抽取已完成但没抽到可判定的条件（上次 {{ currentSummary.last_attempt_at ? formatDate(currentSummary.last_attempt_at) : '—' }}）。
          这不等于"没有条件"——也可能是模型拿不准没抽，需管理员确认。
        </p>
        <!-- 数据与标签冲突时要明说：匹配是真的会跑，不能按"不适用"糊过去 -->
        <p v-if="currentSummary.conflict" class="err-line tiny">
          该文件已标记「不适用于资格匹配」，但仍有 {{ currentSummary.approved_conditions }} 条已复核资格条件，
          两者矛盾——请管理员在「规则管理」里修正标记。
        </p>
        <p v-if="!canMatch" class="muted tiny">
          仍可填写画像并点「开始匹配」，系统会返回这份文件当前的准备情况（而不是空白结果）。
        </p>
      </div>

      <h3 class="sub">我的画像</h3>
      <p class="muted tiny">全部选填。留空的字段会变成「缺少信息」而不是「不符合」——两者含义完全不同。</p>
      <!-- R18：填写进度——一眼看出还差哪几项，而不是扫完九格才意识到漏了 -->
      <div class="fill-hint">
        <span class="fill-track">
          <span class="fill-fill" :style="{ width: `${Math.round((filledCount / fields.length) * 100)}%` }" />
        </span>
        <span class="muted tiny">
          已填写 {{ filledCount }} / {{ fields.length }} 项<template v-if="fields.length - filledCount">，还有 {{ fields.length - filledCount }} 项未填</template>
        </span>
      </div>
      <div v-for="g in fieldsByGroup" :key="g.id" class="field-group">
        <h4 class="group-label">{{ g.label }}</h4>
        <div class="profile-grid">
          <label v-for="f in g.fields" :key="f.key" class="field">
          <span>{{ f.label }}</span>
          <select v-if="f.options" v-model="profile[f.key]" class="select">
            <option value="">未填写</option>
            <option v-for="opt in f.options" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
          </select>
          <!--
            年级：可下拉选，也可以直接输。
            用原生 datalist 而不是自绘下拉面板——自绘的焦点管理、键盘导航、
            点击外部关闭是三处各有各的 bug，而这里只需要 1..N 这几个数字。
            datalist 天生"点开有候选、也可以不理它直接打字"，正好是用户要的交互。
          -->
          <template v-else-if="f.key === 'grade'">
            <input
              :value="profile.grade"
              type="text"
              inputmode="numeric"
              autocomplete="off"
              list="grade-options"
              :placeholder="hintOf(f)"
              :aria-invalid="invalidFields.includes(f.label)"
              :class="{ bad: invalidFields.includes(f.label) }"
              @input="onNumberInput(f, $event)"
              @blur="normalizeNumber(f)"
            />
            <datalist id="grade-options">
              <option v-for="n in match.gradeOptions" :key="n" :value="String(n)">{{ n }} 年级</option>
            </datalist>
          </template>
          <input
            v-else-if="f.type === 'number'"
            :value="profile[f.key]"
            type="text"
            inputmode="decimal"
            autocomplete="off"
            :placeholder="hintOf(f)"
            :aria-invalid="invalidFields.includes(f.label)"
            :class="{ bad: invalidFields.includes(f.label) }"
            @input="onNumberInput(f, $event)"
            @blur="normalizeNumber(f)"
          />
          <input v-else v-model="profile[f.key]" type="text" :placeholder="hintOf(f)" />
        </label>
        </div>
      </div>

      <p class="muted tiny">
        年级的可选范围随学生类型变化：全日制在校专科生 1–{{ GRADE_MAX_BY_TYPE['全日制在校专科生'] }},
        全日制在校本科生 1–{{ GRADE_MAX_BY_TYPE['全日制在校本科生'] }}，其他类型 1–8。
        超出范围会取最近的边界值（输 9 得 4，输 0 得 1），输入框不接受加减号与小数点。
      </p>

      <p v-if="invalidFields.length" class="invalid-line">
        {{ invalidFields.join('、') }} 超出允许范围（绩点 0–5、排名 0–100、学分 0–300），
        请改正后再匹配——系统不会替你把越界值改成边界值。
      </p>

      <div class="actions">
        <button
          class="btn"
          :disabled="matching || !match.documentId || invalidFields.length > 0"
          @click="runMatch"
        >
          {{ matching ? '比对中…' : '开始匹配' }}
        </button>
        <span class="muted tiny">不调用模型：同样的画像每次得到同样的结果。</span>
      </div>
      <p v-if="selectVersions && selectVersions.count > 1" class="version-pre-warn">
        该政策在知识库有 {{ selectVersions.count }} 个版本，判定前请核对使用的是不是现行有效版——
        <router-link :to="`/library?doc=${selectVersions.document_id}`">查看版本列表</router-link>
      </p>
    </section>

    <p v-if="error" class="err-line">{{ error }}</p>

    <section v-if="notice" class="card warn-card">
      <h3>暂时无法匹配</h3>
      <p class="muted">{{ notice }}</p>
      <div v-if="noticeGroups.length" class="group-pick">
        <span class="muted tiny">这份文件里实际标注过的奖项：</span>
        <button
          v-for="g in noticeGroups"
          :key="g"
          class="btn btn-ghost btn-sm"
          :disabled="matching"
          @click="match.targetGroup = g; runMatch()"
        >{{ g }}</button>
      </div>
      <p class="muted tiny">
        管理员需要先在该文件上运行规则抽取，并逐条复核通过——没复核的规则不参与判定，
        否则等于拿模型的一次猜测去回答"你符不符合条件"。
      </p>
    </section>

    <template v-if="result">
      <!-- R15：版本核对提醒。有多个版本时先说版本，再说结论 -->
      <section v-if="result.version_note" class="card version-warn">
        <p>
          <strong>该政策存在 {{ result.version_note.count }} 个版本</strong>
          <span class="muted">——{{ result.version_note.hint }}</span>
        </p>
      </section>
      <section class="card summary">
        <div>
          <h3>匹配结论</h3>
          <p class="muted tiny">{{ result.disclaimer }}</p>
          <p v-if="result.target_group" class="muted tiny">
            已按「{{ result.target_group }}」过滤：{{ result.filtered_out ?? 0 }} 条其他奖项的条件未参与本次判定。
          </p>
          <p v-else class="muted tiny">
            未区分奖项：这份文件里全部已复核条件都参与了判定，混着看需自行区分适用哪一套。
          </p>
        </div>
        <!--
          三类计数同时就是筛选开关：点击切换某一类显不显示，可多选。
          不另起一行筛选器——三个数字本来就在最显眼的位置，
          再画一组一模一样的 chip 只是把同一件事说两遍。
        -->
        <div class="counts" role="group" aria-label="按结论筛选">
          <button
            v-for="s in ALL_STATUSES"
            :key="s"
            type="button"
            :class="['count', MATCH_TONE[s], { off: !shownStatuses.includes(s) }]"
            :aria-pressed="shownStatuses.includes(s)"
            :title="shownStatuses.includes(s) ? '点一下隐藏这一类' : '点一下显示这一类'"
            @click="toggleStatus(s)"
          >{{ MATCH_LABEL[s] }} {{ result.summary[s] }}</button>
        </div>
      </section>
      <p class="filter-note muted tiny">
        点上面三类可以只看其中几类（可多选）。
        <template v-if="hiddenTotal">当前已隐藏 {{ hiddenTotal }} 条。</template>
      </p>

      <p v-if="!grouped.length" class="empty-hint muted small">
        三类都被隐藏了，点上面任意一个即可恢复显示。
      </p>

      <section v-for="group in grouped" :key="group.status" class="card">
        <h3 :class="['group-title', MATCH_TONE[group.status]]">
          {{ MATCH_LABEL[group.status] }}（{{ group.items.length }}）
        </h3>
        <ul class="items">
          <li v-for="item in group.items" :key="item.rule_id">
            <div class="item-head">
              <span :class="['tag', MATCH_TONE[item.status]]">{{ MATCH_LABEL[item.status] }}</span>
              <span class="tag plain">{{ RULE_TYPE_LABEL[item.rule_type] ?? item.rule_type }}</span>
              <span v-if="item.unknown_reason" class="tag unknown">
                {{ UNKNOWN_REASON_LABEL[item.unknown_reason] ?? item.unknown_reason }}
              </span>
            </div>
            <!--
              R8：一条结果只说两句话——「这条要求什么」与「你为什么符合/不符合」。
              「你的值 / 政策要求」那行删掉：rationale 里已经带着具体数值
              （"年级 4 满足要求不低于 2，符合"），再列一遍是同一件事说两遍。
              政策原文降级成一行可展开的入口：默认不占视觉，
              但"能不能核对"这件事必须留着——匹配的全部价值就在可追溯。
            -->
            <p class="desc">{{ item.description }}</p>
            <p class="rationale">{{ item.rationale }}</p>
            <details class="evidence">
              <summary>政策原文</summary>
              <div class="muted tiny">{{ evidenceLabel(item.evidence.page, item.evidence.section) }}</div>
              <blockquote class="quote">{{ item.evidence.quote }}</blockquote>
            </details>
          </li>
        </ul>
      </section>

      <section class="card">
        <h3>下一步：生成申请计划</h3>
        <p class="muted small">
          按政策里的材料清单、截止时间与缺项信息，自动起草一份任务清单。草稿不会写进任务中心，
          确认之后才落库。
        </p>
        <div class="actions">
          <button class="btn" :disabled="drafting" @click="makeDraft">
            {{ drafting ? '生成中…' : '生成计划草稿' }}
          </button>
        </div>
        <p v-if="confirmedId" class="ok-line">
          计划已确认（{{ confirmedId }}），到「计划与任务」页可以推进任务状态。
        </p>
      </section>

      <!-- R16 纠错入口：匹配结论依据的政策内容有误时上报 -->
      <p class="match-correction">
        <button class="link" @click="correctionOpen = true">报告此处有误</button>
        <span class="muted tiny">——匹配结论依据的政策内容有误？提交后管理员会核对。</span>
      </p>
    </template>

    <section v-if="draft" class="card">
      <h3>计划草稿：{{ draft.title }}</h3>
      <p class="muted tiny">{{ draft.note }}</p>
      <ol class="draft-tasks">
        <li v-for="t in draft.tasks" :key="t.temp_id">
          <div class="item-head">
            <span class="tag plain">{{ t.priority }}</span>
            <strong>{{ t.title }}</strong>
            <span v-if="t.needs_human_confirmation" class="tag unknown">需人工确认</span>
          </div>
          <p v-if="t.description" class="muted small">{{ t.description }}</p>
          <p class="muted tiny">
            截止：{{ t.due_at ? t.due_at.slice(0, 10) : '政策原文未给出，需自行确认' }}
            <template v-if="t.required_materials"> · 材料：{{ t.required_materials }}</template>
          </p>
        </li>
      </ol>
      <div v-if="draft.notes.length" class="notes">
        <p v-for="n in draft.notes" :key="n" class="muted tiny">· {{ n }}</p>
      </div>
      <div class="actions">
        <button class="btn" :disabled="confirming" @click="confirmDraft">
          {{ confirming ? '确认中…' : `确认并写入任务中心（${draft.tasks.length} 项）` }}
        </button>
        <button class="btn btn-ghost" :disabled="confirming" @click="draft = null">放弃草稿</button>
      </div>
    </section>

    <!-- R16 纠错反馈弹层：资格匹配结果 -->
    <CorrectionModal
      v-if="result"
      :open="correctionOpen"
      target-type="match"
      :target-id="result.session_id"
      :document-id="result.document_id"
      :target-title="currentDoc?.title ?? '匹配结果'"
      @close="correctionOpen = false"
    />
  </div>
</template>

<style scoped>
.match-correction {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin: var(--space-2) 0 0;
}
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.small {
  font-size: 12px;
}

.tiny {
  font-size: 11px;
}

.row {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.grow {
  flex: 1;
  min-width: 240px;
}

.award {
  min-width: 220px;
}

.group-pick {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
  margin: var(--space-2) 0;
}

.btn-sm {
  min-height: 30px;
  padding: 0 10px;
  font-size: 12px;
}

.select {
  min-height: 40px;
  padding: 0 var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
  font-family: inherit;
  font-size: 14px;
  color: var(--c-text);
}

.sub {
  margin: var(--space-4) 0 var(--space-2);
  font-size: 14px;
}

.profile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-3);
}

/* ---------- R18：画像分组 + 填写进度 ---------- */
.field-group {
  margin-top: var(--space-4);
}

.group-label {
  margin: 0 0 var(--space-1);
  font-size: 12px;
  font-weight: 600;
  color: var(--c-text-2);
}

.field-group .profile-grid {
  margin-top: var(--space-1);
}

.fill-hint {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-3);
}

.fill-track {
  flex: 1;
  max-width: 260px;
  height: 6px;
  border-radius: 999px;
  background: var(--c-surface-2);
  overflow: hidden;
}

.fill-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--c-primary);
  transition: width 0.3s ease;
}

/* 数值字段是 text + inputmode="decimal"，所以原生上下箭头压根不存在——
   不用再跟系统浅色 spinner 对抗。这里只补两件事：等宽数字对齐、越界描边。 */
.profile-grid input[inputmode="decimal"] {
  font-variant-numeric: tabular-nums;
}

.profile-grid input.bad {
  border-color: var(--c-not-met-border);
}

.invalid-line {
  margin: var(--space-2) 0 0;
  color: var(--c-not-met);
  font-size: 12.5px;
  line-height: 1.7;
}

/* R21：可匹配政策为空时的空状态。不用大段留白——给出"为什么空"和三条下一步 */
.match-empty {
  padding: var(--space-6) var(--space-4);
  text-align: center;
  border: 1px dashed var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
}
.match-empty-icon {
  display: block;
  font-size: 26px;
  line-height: 1;
  color: var(--c-text-2);
  margin-bottom: var(--space-2);
}
.match-empty-title {
  margin: 0 0 var(--space-2);
  font-size: 14px;
  font-weight: 600;
  color: var(--c-text);
}
.match-empty p {
  margin: 0 0 var(--space-2);
  max-width: 620px;
  margin-inline: auto;
  line-height: 1.75;
}
.match-empty p:last-child {
  margin-bottom: 0;
}

/* R20：文件准备度提示块。左边框取状态的色调，但**状态同时有文字标签**，
   颜色只是第二层线索——只靠颜色区分状态，对色觉障碍用户等于没有提示。 */
.rule-state {
  margin: var(--space-3) 0 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--c-border);
  border-left: 3px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
}
.rule-state.tone-met {
  border-left-color: var(--c-met);
}
.rule-state.tone-unknown {
  border-left-color: var(--c-unknown);
}
.rule-state.tone-not_met {
  border-left-color: var(--c-not-met);
}
.rule-state-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.rule-state-hint {
  margin: var(--space-2) 0 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--c-text-2);
}
.rule-state p:last-child {
  margin-bottom: 0;
}

.actions {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  margin-top: var(--space-4);
  flex-wrap: wrap;
}

.err-line {
  color: var(--c-not-met);
  font-size: 13px;
  margin: 0;
}

.ok-line {
  color: var(--c-met);
  font-size: 13px;
  margin: var(--space-2) 0 0;
}

.warn-card {
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.counts {
  display: flex;
  gap: var(--space-2);
}

.count {
  padding: 2px 12px;
  border-radius: 999px;
  font-size: 13px;
  border: 1px solid var(--c-border);
  /* R8：三类计数从 span 改成 button（点一下即筛选），需要把浏览器默认外观清掉 */
  font-family: inherit;
  background: none;
  color: var(--c-text);
  cursor: pointer;
  transition: opacity 0.15s ease, border-color 0.15s ease;
}

.count:hover {
  border-color: var(--c-border-strong);
}

/* 被筛掉的那一类：压到 35% 并加删除线，和"这一类本来就没有"区分开——
   后者计数是 0，前者计数还在、只是没显示。 */
.count.off {
  opacity: 0.35;
  border-style: dashed;
  text-decoration: line-through;
}

.filter-note {
  margin: -10px 0 0;
  padding: 0 2px;
}

/* 三类全被筛掉时的空态：不要用 .card —— 一张只有一行字的卡片看起来像加载失败 */
.empty-hint {
  margin: 0;
  padding: var(--space-3);
  border: 1px dashed var(--c-border);
  border-radius: var(--radius-card);
  text-align: center;
}

.count.met {
  color: var(--c-met);
  border-color: var(--c-met-border);
  background: var(--c-met-bg);
}

.count.not_met {
  color: var(--c-not-met);
  border-color: var(--c-not-met-border);
  background: var(--c-not-met-bg);
}

.count.unknown {
  color: var(--c-unknown);
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.group-title {
  font-size: 14px;
  margin-bottom: var(--space-3);
}

.group-title.met {
  color: var(--c-met);
}

.group-title.not_met {
  color: var(--c-not-met);
}

.group-title.unknown {
  color: var(--c-unknown);
}

.items,
.draft-tasks {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.items li {
  border-bottom: 1px dashed var(--c-border);
  padding-bottom: var(--space-3);
}

.items li:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.item-head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
}

.desc {
  margin: var(--space-2) 0 var(--space-1);
  font-size: 13px;
}

.rationale {
  margin: 0 0 var(--space-2);
  font-size: 13px;
  color: var(--c-text-2);
}

/* R8：政策原文收进 details，默认不占视觉——一条结果只留「要求什么」+「你为什么符合」。
   展开才看得到出处，但出处必须还在：匹配的全部价值就是可追溯。 */
.evidence {
  margin-top: var(--space-2);
}

.evidence summary {
  width: fit-content;
  cursor: pointer;
  font-size: 11.5px;
  color: var(--c-text-2);
}

.evidence summary:hover {
  color: var(--c-primary);
}

.quote {
  margin: var(--space-2) 0 0;
  padding: var(--space-2) var(--space-3);
  background: var(--c-surface-2);
  border-left: 3px solid var(--c-primary-weak);
  font-size: 12px;
  color: var(--c-text-2);
  line-height: 1.75;
}

.tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--c-border);
  white-space: nowrap;
}

.tag.met {
  color: var(--c-met);
  border-color: var(--c-met-border);
  background: var(--c-met-bg);
}

.tag.not_met {
  color: var(--c-not-met);
  border-color: var(--c-not-met-border);
  background: var(--c-not-met-bg);
}

.tag.unknown {
  color: var(--c-unknown);
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.tag.plain {
  color: var(--c-text-2);
  background: var(--c-bg);
}

.notes {
  margin-top: var(--space-3);
}

.draft-tasks li {
  padding: var(--space-2) 0;
  border-bottom: 1px dashed var(--c-border);
}

.draft-tasks li:last-child {
  border-bottom: none;
}

/* ---------- R15：版本提醒 ---------- */
.version-warn {
  border-left: 3px solid var(--c-warn-border);
  background: var(--c-warn-bg);
}
.version-warn p {
  margin: 0;
  font-size: 12.5px;
}
.version-pre-warn {
  margin: var(--space-2) 0 0;
  font-size: 12px;
  color: var(--c-unknown);
}
</style>
