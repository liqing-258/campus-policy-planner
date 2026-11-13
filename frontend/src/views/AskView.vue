<script setup lang="ts">
/**
 * 智能问答（M4 建、M6 补全、M7 改为对话式布局、R3 补「切页不丢对话」）。
 *
 * 布局参照常见对话式助手：解答在上方主区、提问框固定在底部、
 * 右侧常驻「我提问过的问题」。改布局时**没有删掉任何一项验收能力**：
 *
 * 1. 答得出来 —— 正文 + 每条结论后面的 [n]，点开能看到原文
 * 2. 答不出来就拒答 —— 明确说"证据不足"，并列出被闸门拦下的候选
 * 3. 未配模型时降级 —— 只给原文片段，不假装给过归纳
 * 4. 节点轨迹是真的 —— 执行过几个节点就显示几个"完成"，
 *    没执行的（matching / planning）显示灰的"未执行"并给原因
 * 5. 可疑内容必须显形 —— 版本冲突并列展示、疑似注入的片段带标记
 *
 * 两条交互上的取舍写在这里，免得后来人以为是漏做：
 * - **建议问题只在要问的时候出现**：一进页面就铺三条问题，看起来像引导用户
 *   照着念的假界面。只有聚焦输入框（或刚答完一轮）才浮出来。
 * - **历史问题点击只填入、不自动提交**：它是"我想再问一遍"，
 *   直接发出去等于替用户做决定，还会覆盖掉他正在看的上一条解答。
 *
 * **R3：为什么"这一轮"的东西要挪进 store**
 *
 * 原来 answer / refusal / degraded 是本组件的三个 ref，`<router-view>` 一切走
 * 组件就卸载，三个 ref 一起消失——用户点了「政策知识库」再点回来，刚才那段解答
 * 变成一片欢迎页，而数据其实一直在库里。这是真实缺陷，不是"预期行为"：
 * 同一份会话在右侧历史栏里明明还列着。
 *
 * 改成"消息列表 + 当前会话 id"存进 Pinia（`stores/qa.ts`，落 sessionStorage）之后：
 * - 卸载时**不清空**（`onBeforeUnmount` 里没有任何 clear）；
 * - 只有点「新对话」才清空；
 * - 挂载时先 restore → 空且有 conversation_id 就查接口 → 还空才显示欢迎区。
 *
 * 本地缓存只存 role / content / 时间 / 精简引用（见 store 里的说明），所以
 * 切走再切回来时：**一轮完整对话（问答成对）能原样显示**，包括点开得动的引用；
 * 但"这次处理过程"折叠面板与版本冲突表只活在当前页，回来就收起了——
 * 它们体积大且刷新即过期，硬塞进 5MB 的 sessionStorage 得不偿失。
 *
 * 三个 store 之外仍然留在组件里的状态，以及为什么：
 * - `answer / refusal / degraded / citationRejected / fatal`：渲染当前这一轮用的**完整**
 *   对象（节点轨迹、版本冲突、被拒草稿都在里面）。store 里那份是"重放用的精简版"，
 *   两者刻意不合并——一个要全、一个要小。
 * - `askedQuestion`：决定"这一轮问的是哪句"，与 store 的追加顺序解耦。
 * - 反馈状态（`feedbackSent` 等）：挂在 `message_id` 上，跨页没有意义。
 *
 * 4001/4003/4006 都是 HTTP 200 的业务态，所以用 api.raw 自己按 code 分支，
 * 不能像普通接口那样 try/catch 当错误处理。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ApiError,
  api,
  type AnswerData,
  type Candidate,
  type CitationRejectedData,
  type ConversationRow,
  type DegradedData,
  type FeedbackReason,
  type Paged,
  type RefusalData,
  type Workflow,
} from '../api/client'
import {
  FEEDBACK_REASON_LABEL,
  RUN_OUTCOME_LABEL,
  EFFECTIVE_LABEL,
  evidenceLabel,
  formatTime,
  nodeStatus,
} from '../utils/format'
import { fromServerMessage, useQAStore, type QAMessage } from '../stores/qa'

const router = useRouter()
const route = useRoute()
const qa = useQAStore()

const question = ref('')
/** 这一轮实际问出去的问题。单独存是因为输入框随时会被改成下一条 */
const askedQuestion = ref('')
const asking = ref(false)
const elapsed = ref(0)

/**
 * 这一轮的业务态原文。store 里的 `messages` 是落地版（给缓存与重放用），
 * 这几个 ref 是"渲染版"——它们带着完整字段（节点轨迹、版本冲突、被拒草稿），
 * 只活在这一页。两者刻意不合并：一个是持久、精简；一个是临时、完整。
 */
const answer = ref<AnswerData | null>(null)
const refusal = ref<RefusalData | null>(null)
const degraded = ref<DegradedData | null>(null)
const citationRejected = ref<CitationRejectedData | null>(null)
const fatal = ref('')
/** 会话 id 的真相在 store 里——它要跨页活着 */
const conversationId = computed(() => qa.currentConversationId)
/**
 * 挂载时把缓存/接口的旧对话也铺出来——此时上面那几个"渲染版" ref 还是空的。
 *
 * 注意它**不再**决定"铺不铺历史"：历史一律由 `qa.messages` 减去本轮两条得出。
 * 早先用这个布尔量当门，结果"切回来铺好了历史 → 再追问一句"历史立刻消失，
 * 而这正是用户要看的连续对话。
 */

const openCitation = ref('')
/** 历史轮次里展开的引用（键是 `${轮次下标}-${编号}`，缓存里没有 chunk_id） */
const openPast = ref('')

const conversations = ref<ConversationRow[]>([])
/** 右上角「最近提问」控制右侧栏。默认展开：它就是豆包那种右侧历史 */
const historyOpen = ref(true)
/** 输入框是否聚焦——决定建议问题显不显示 */
const focusOpen = ref(false)
const inputRef = ref<HTMLTextAreaElement | null>(null)
const scroller = ref<HTMLElement | null>(null)

// ---------- 反馈 ----------
const ratingFor = ref('')
const ratingReason = ref<FeedbackReason | ''>('')
const feedbackSent = ref<Record<string, { helpful: boolean; reason_code: string | null }>>({})
const feedbackBusy = ref(false)
const feedbackError = ref('')

const reasons = Object.keys(FEEDBACK_REASON_LABEL) as FeedbackReason[]

async function submitFeedback(messageId: string, helpful: boolean) {
  if (!messageId || feedbackBusy.value) return
  if (!helpful && !ratingReason.value) {
    // 后端也拦（1001），但在前端先拦一次：用户点完立刻知道要做选择，不用等一个来回
    feedbackError.value = '标记为「没帮助」时请选一个原因，否则这条反馈没法用来改进'
    return
  }
  feedbackBusy.value = true
  feedbackError.value = ''
  try {
    const result = await api.post<{
      feedback_id: string
      helpful: boolean
      reason_code: string | null
      updated: boolean
    }>(`/messages/${messageId}/feedback`, {
      helpful,
      reason_code: helpful ? null : ratingReason.value || null,
    })
    feedbackSent.value[messageId] = {
      helpful: result.helpful,
      reason_code: result.reason_code,
    }
    ratingFor.value = ''
    ratingReason.value = ''
  } catch (err) {
    feedbackError.value = err instanceof Error ? err.message : '反馈没提交成功'
  } finally {
    feedbackBusy.value = false
  }
}

function feedbackOf(messageId: string) {
  return feedbackSent.value[messageId] ?? null
}

// ---------- 提问 ----------
async function ask(text?: string) {
  const payload = (text ?? question.value).trim()
  if (!payload || asking.value) return
  question.value = payload
  askedQuestion.value = payload
  asking.value = true
  elapsed.value = 0
  // 清掉"这一轮"的渲染态：上一轮已经在 qa.messages 里，会作为"上文"继续显示，
  // 不必靠这几个 ref 留着重放——它们只负责当前这一轮。
  answer.value = null
  refusal.value = null
  degraded.value = null
  citationRejected.value = null
  fatal.value = ''
  openCitation.value = ''
  openPast.value = ''
  ratingFor.value = ''
  ratingReason.value = ''
  feedbackError.value = ''
  const timer = window.setInterval(() => (elapsed.value += 100), 100)

  /**
   * 什么时候续在原会话里：**只要当前有会话就续**。
   *
   * R3 起改成这一条——这是多轮对话的前提：用户点「继续追问」时拼出来的句子
   * 含"这个问题"，只有后端能读到上文，改写才还原得出它指的是哪件事。
   * 原来的实现只在"原样重问"时才带 id，于是每次追问都新建一条会话，
   * 一问一答各成孤岛，指代词永远没处还原。
   *
   * 早先版本还要求 `qa.messages` 非空，判据是"这条会话里已经有问答在铺着"。
   * 那个判据是错的：用户在右侧点了一条旧会话再发言时，messages 是空的
   * （刚切过去还没拉），于是"点名要续那条会话"反而被当成新会话。
   * 会话 id 本身就是"要不要续"的唯一真相。
   */
  const continuation = Boolean(conversationId.value)
  const body: Record<string, unknown> = { question: payload }
  if (continuation) body.conversation_id = conversationId.value
  else {
    /*
     * 不续的话必须**立刻断开**旧会话 id。
     *
     * 这里有一个很容易漏的坑：`fillFromHistory()` 会把用户点过的那条历史会话 id
     * 摆进 store（好让"发送"续进去）。若用户点了历史、改了个完全不同的问题再发，
     * 只靠下面 `if (先) 就覆盖 id`，新问题的会话 id 确实会被换掉——
     * 但那之前它已经以旧会话的身份发出去了，新问题被塞进别人的会话里，
     * 而且拿到的是旧会话的上文（改写会把上一件事的实体补进这个新问题）。
     * 所以"不续"这一支要在发请求之前就把 id 清掉。
     */
    qa.setConversationId('')
  }

  // 提问先落进消息列表：用户切走再回来，至少看得到"我问过什么"
  qa.appendMessage({
    role: 'user', content: payload, timestamp: new Date().toISOString(), kind: 'answer',
  })

  try {
    const envelope = await api.raw<unknown>('/qa/ask', body)
    if (envelope.code === 0) {
      answer.value = envelope.data as AnswerData
      qa.setConversationId(answer.value.conversation_id)
      commitTurn('answer', answer.value.answer, answer.value.citations)
      qa.setWorkflowTrace(answer.value.workflow ?? null)
    } else if (envelope.code === 4001) {
      refusal.value = envelope.data as RefusalData
      // **只在拿到非空 id 时覆盖**：拒答只是"这一轮没有答案"，会话还在。
      // 早先无条件写 `?? ''`，一被拒答就把会话 id 清空，下一句追问被迫另起会话，
      // 上一轮问题不在历史里，改写再强也无从还原指代词。
      if (refusal.value.conversation_id) {
        qa.setConversationId(refusal.value.conversation_id)
      }
      commitTurn('refusal', refusal.value.reason, [])
      qa.setWorkflowTrace(refusal.value.workflow ?? null)
    } else if (envelope.code === 4006) {
      degraded.value = envelope.data as DegradedData
      if (degraded.value.conversation_id) {
        qa.setConversationId(degraded.value.conversation_id)
      }
      // 降级没有归纳结论，只有原文片段：写成消息正文，切回来才读得到内容。
      // 只写前两条 + 一句说明：这里存的是"回忆用的摘要"，不是数据源，
      // 整段拼接会把一条消息撑到几 KB（store 里另有裁剪，但没必要先撑大）。
      const shown = degraded.value.candidates.slice(0, 2)
        .map((c) => `[${c.index}] ${c.document_title}：${(c.content ?? '').slice(0, 160)}`)
        .join('\n\n')
      commitTurn('degraded',
        shown
          + `\n\n（降级模式共 ${degraded.value.candidates.length} 条原文片段，完整内容见下方列表）`,
        degraded.value.candidates)
      qa.setWorkflowTrace(degraded.value.workflow ?? null)
    } else if (envelope.code === 4003) {
      citationRejected.value = envelope.data as CitationRejectedData
      // 这一轮没被采纳，会话 id **不能**覆盖成空——否则后续追问会另起一条会话
      commitTurn('citation_rejected', '这条解答被拦下：引用编号越界，无法核验。', [])
      qa.setWorkflowTrace(citationRejected.value.workflow ?? null)
    } else {
      fatal.value = `${envelope.code} · ${envelope.message}`
      commitTurn('fatal', fatal.value, [])
    }
  } catch (err) {
    // 4002/4004/4005 是真故障，必须和"没找到依据"区分开
    fatal.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '请求失败')
    commitTurn('fatal', fatal.value, [])
  } finally {
    window.clearInterval(timer)
    asking.value = false
    // 写完这一轮立刻落盘：防抖只该省掉中途的多次写，不该让"最后一条"也悬着
    qa.writeNow()
    await loadConversations()
    await nextTick()
    scroller.value?.scrollTo({ top: scroller.value.scrollHeight, behavior: 'smooth' })
  }
}

/** 把这一轮的助手侧内容落进消息列表（缓存与重放都读它） */
function commitTurn(kind: QAMessage['kind'], content: string, citations: Candidate[]) {
  qa.appendMessage({
    role: 'assistant',
    content: content || '',
    timestamp: new Date().toISOString(),
    kind,
    citations: citations
      .filter((c) => c.document_title)
      .map((c) => ({
        index: c.index,
        document_id: c.document_id,
        document_title: c.document_title,
        section_title: c.section_title ?? null,
        page_number: c.page_number ?? null,
        snippet: String(c.content ?? '').slice(0, 160),
      })),
  })
}

async function loadConversations() {
  try {
    const data = await api.get<Paged<ConversationRow>>('/conversations?page=1&page_size=30')
    conversations.value = data.items
  } catch {
    /* 历史列表失败不该盖住主结果，静默即可 */
  }
}

/**
 * 历史问题只填入输入框，不自动提交。
 * 否则用户点一下历史，正在看的上一条解答就被冲掉了。
 *
 * 但它会把会话 id 摆进 store，并且**切走时清空当前消息列表**：
 * 再点发送时这一问要续进那条会话，而列表里留着的必须是那条会话的内容，
 * 不能是上一条会话的残留（否则缓存里的两段对话会被拼成一段并不存在的历史）。
 */
function fillFromHistory(row: ConversationRow) {
  question.value = row.title
  qa.switchConversation(row.id)
  focusOpen.value = true
  nextTick(() => {
    inputRef.value?.focus()
    autoGrow()
  })
}

/**
 * 挂载时的恢复顺序，三步，任何一步拿到内容就停：
 *
 * 1. `restore()` —— 本标签页的 sessionStorage 缓存（切页回来走这条，瞬间可见）
 * 2. 缓存空但记得 `conversation_id` —— 查接口补历史（清了缓存、换了标签页走这条）
 * 3. 都空 —— 显示欢迎区（真的没聊过）
 *
 * `restore()` 内部已有 try-catch（坏缓存会自清并返回 null）；这里再包一层，
 * 是因为"缓存读得出来、但接口挂了"同样不能把页面弄白。
 */
async function restoreSession() {
  const cached = qa.restore()
  // 从知识库带 query 跳进来时，URL 上可能带着会话 id
  const fromUrl = typeof route.query.conversation_id === 'string'
    ? route.query.conversation_id : ''

  if (cached?.messages.length) {
    await nextTick()
    scrollToEnd()
    return
  }

  const targetId = cached?.conversationId || fromUrl
  if (!targetId) return
  try {
    if (await qa.hydrate(targetId)) {
      await nextTick()
      scrollToEnd()
    }
  } catch {
    /* 历史拉不回来就当没有——欢迎区总比白屏好 */
  }
}

function scrollToEnd() {
  scroller.value?.scrollTo({ top: scroller.value.scrollHeight })
}

/**
 * 新建对话：清缓存、清这一轮的渲染态，回到欢迎区。
 * **这是唯一允许清空 store 的入口**——卸载时绝不调它。
 */
function newConversation() {
  if (asking.value) return
  qa.clear()
  answer.value = null
  refusal.value = null
  degraded.value = null
  citationRejected.value = null
  fatal.value = ''
  askedQuestion.value = ''
  question.value = ''
  openCitation.value = ''
  openPast.value = ''
  // 反馈是挂在 message_id 上的，换了会话就不能再显示上一轮的"已评价"
  ratingFor.value = ''
  ratingReason.value = ''
  feedbackSent.value = {}
  feedbackError.value = ''
  nextTick(() => inputRef.value?.focus())
}

/**
 * 铺出来的历史轮次。
 *
 * 只取"当前这一轮之前"的部分：本轮那一对（提问 + 解答）由下方结构化区块渲染，
 * 再铺一遍就重复了。判据是本轮是否有渲染态——有就把它对应的两条从末尾切掉。
 *
 * **不再用"是否刚从缓存恢复"当开关**（早先有一个 `historyLoaded`）：那个开关让
 * "切回来看到历史 → 再追问一句"时历史立刻消失，而这恰恰是用户要看的连续对话。
 * 现在历史只由消息列表本身决定，恢复的和刚问完的走同一条路径。
 */
const historyTurns = computed<QAMessage[]>(() => {
  const list = qa.messages
  const hasCurrentTurn = Boolean(answer.value || refusal.value || degraded.value
    || citationRejected.value || fatal.value || askedQuestion.value)
  if (!hasCurrentTurn) return list
  // 本轮最多占末尾两条（提问 + 解答）；按角色从后往前剥，不按位置硬切
  let end = list.length
  if (end > 0 && list[end - 1].role === 'assistant') end -= 1
  if (end > 0 && list[end - 1].role === 'user') end -= 1
  return list.slice(0, end)
})

// ---------- 结果汇总 ----------
/** 四条分支都可能带轨迹（包括拒答与降级），统一取一次 */
const workflow = computed<Workflow | null>(() => answer.value?.workflow
  ?? refusal.value?.workflow
  ?? degraded.value?.workflow
  ?? citationRejected.value?.workflow
  ?? null)

const conflicts = computed(() => answer.value?.conflicts
  ?? refusal.value?.conflicts
  ?? degraded.value?.conflicts
  ?? citationRejected.value?.conflicts
  ?? [])

const injections = computed(() => answer.value?.injection_suspected
  ?? refusal.value?.injection_suspected
  ?? degraded.value?.injection_suspected
  ?? [])

const hasResult = computed(() => Boolean(answer.value || refusal.value || degraded.value
  || citationRejected.value || fatal.value))

/**
 * 一次解答都没有、也没在问、也没有铺出来的历史——才显示欢迎区。
 *
 * 加了 `historyTurns.length` 这一项是关键：切页回来时上面那几个"渲染版" ref 必然是空的，
 * 只看它们会把一条有内容的会话判成"首次访问"，于是刚恢复的记录又被欢迎页盖住。
 */
const firstVisit = computed(() => !hasResult.value && !asking.value && !askedQuestion.value
  && historyTurns.value.length === 0)

/** 先把 HTML 转义再加粗——模型输出不可信，不能直接塞进 v-html */
function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

/**
 * 只处理 **粗体**：模型爱用它标条件名，原样显示会看到一堆星号。
 * 先转义再加粗，顺序不能反——反过来会把 <strong> 自己的尖括号也转义掉。
 * 历史轮次也走这个函数（见 renderText），免得"刚答完"与"切回来"排版不一样。
 */
function renderText(raw: string): string {
  return escapeHtml(raw).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
}

const answerHtml = computed(() => renderText(answer.value?.answer ?? ''))

const confidenceLabel: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}

// ---------- 建议问题 ----------
const examples = [
  '申请国家励志奖学金需要满足哪些条件？',
  '国家励志奖学金的申请截止时间是哪天？',
  '家庭经济困难认定需要提交哪些材料？',
]

const matterLabel = computed(() => answer.value?.matter.label ?? '这项事务')

const sources = computed(() => {
  const list: Candidate[] = answer.value?.citations
    ?? refusal.value?.rejected_candidates
    ?? degraded.value?.candidates
    ?? []
  const map = new Map<string, string>()
  for (const item of list) {
    if (item.document_id && !map.has(item.document_id)) {
      map.set(item.document_id, item.document_title)
    }
  }
  return Array.from(map, ([id, title]) => ({ id, title }))
})

/**
 * 追问建议：从这一轮的结果里长出来，不是固定三条。
 * 有版本冲突就先问版本；拒答时直接用后端给的"可以试试"。
 */
/**
 * 只保留与本轮引用文件相关的冲突。
 * 后端给的是**知识库级**清单（库里有哪几组同主题多版本），全挂上来会变成噪音：
 * 问图书馆借几本书，解答下面却顶着一张"奖学金 2025/2026 两版"的黄色警告。
 */
const shownConflicts = computed(() => conflicts.value.filter((c) =>
  sources.value.some((s) => s.title.includes(c.topic) || c.topic.includes(s.title))))

const relatedConflict = computed(() => shownConflicts.value[0])

const followUps = computed<string[]>(() => {
  if (!hasResult.value) return []
  const label = matterLabel.value
  const doc = sources.value[0]?.title
  const out: string[] = []

  if (relatedConflict.value) {
    out.push(`新旧两版《${relatedConflict.value.topic}》规定不一致时，以哪一版为准？`)
  }
  if (refusal.value) {
    // 拒答时后端会把"被拦下的候选"变成一条书名号追问，点了就能看到那份文件的原文
    out.push(...refusal.value.suggestions)
    return Array.from(new Set(out.filter((s) => s.trim().length > 0))).slice(0, 3)
  }

  /**
   * **R5：优先用后端派生的追问。**
   *
   * 早先这三条是前端用意图标签拼的（`{label}需要准备哪些材料？`），
   * 而标签是分类器的输出、不是语料承诺：问「课程考核怎么算平时分」得到
   * 「学分与成绩的申请截止时间和办理流程是什么？」——这句话在任何文件里都找不到答案，
   * 点下去必然拒答。实测 18 份文件 × 3 条，这类"点下去没内容"的占 9 条。
   *
   * 后端现在按「刚答的这份文件 + 它真实存在的章节」派生（`build_next_questions`），
   * 每一条都有确定性依据。只有在旧后端不返回该字段时，才退回下面的本地模板。
   */
  const fromServer = answer.value?.next_questions ?? degraded.value?.next_questions ?? []
  if (fromServer.length) {
    out.push(...fromServer)
    return Array.from(new Set(out.filter((s) => s.trim().length > 0))).slice(0, 3)
  }

  /**
   * 兜底模板（旧后端）。「其他事项」是意图分类没认出具体名目时的占位说法，
   * 拿它造句会得到"其他事项需要准备哪些材料？"——读着像废话，点下去也答不出东西，
   * 所以这一支换成文件名。
   */
  const vague = /^其他/.test(label)
  if (vague && doc) {
    out.push(`《${doc}》里还有哪些与这个问题相关的规定？`)
    out.push(`《${doc}》对办理流程和时间要求是怎么规定的？`)
    out.push(`不符合《${doc}》规定的情况会怎么处理？`)
  } else {
    out.push(`${label}需要准备哪些材料？`)
    out.push(`${label}的申请截止时间和办理流程是什么？`)
    out.push(doc
      ? `《${doc}》里还有哪些与${label}相关的规定？`
      : `不满足${label}的条件，还有哪些替代渠道？`)
  }
  return Array.from(new Set(out.filter((s) => s.trim().length > 0))).slice(0, 3)
})

const suggestItems = computed(() => (hasResult.value ? followUps.value : examples))
/**
 * 建议问题的显隐规则，两条要求叠在一起：
 * - 还没问过 → 只有聚焦输入框才出现（不做"一进页面就铺三条"的引导感）
 * - 已经答过一轮 → 常驻，让用户看得见"还能接着问什么"
 */
const suggestOpen = computed(() => {
  if (!suggestItems.value.length) return false
  if (focusOpen.value) return true
  return Boolean(answer.value || refusal.value || degraded.value || citationRejected.value)
})
const suggestTitle = computed(() => (hasResult.value ? '继续追问' : '你可以这样问'))

/** 点建议＝真的要问，直接提交（与"点历史只填入"刻意不同） */
function useSuggestion(text: string) {
  question.value = text
  ask(text)
}

function autoGrow() {
  const el = inputRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 132)}px`
}

function goAction(action: string) {
  router.push(action === 'match' ? '/match' : '/plans')
}

onMounted(async () => {
  await loadConversations()
  await restoreSession()
})

/**
 * **卸载时不允许清空会话。**
 *
 * 这里只留一句注释和一次"把待写的防抖冲掉"：切页时如果再等 300ms 就没机会写了，
 * 所以强制落盘一次。除此之外什么都不做——`qa.clear()` 只属于「新建对话」按钮
 * 和登出流程，它出现在这里正是"切页丢对话"这个缺陷的成因。
 */
onBeforeUnmount(() => {
  qa.writeNow()
})
</script>

<template>
  <div class="chat" :class="{ 'no-side': !historyOpen }">
    <header class="chat-head">
      <div class="chat-title">
        <h2>智能问答</h2>
        <p class="muted small">
          解答里的每个 [n] 都对应知识库中一条真实片段，点开可以核对原文；
          证据不足时系统会直接说不知道，而不是编一个看起来合理的说法。
        </p>
      </div>
      <button
        v-if="qa.messages.length"
        class="btn btn-ghost btn-sm new-chat"
        title="清空当前对话，开始一个新的问题"
        @click="newConversation"
      >
        <span class="ico">＋</span>
        新对话
      </button>
      <button
        class="btn btn-ghost btn-sm history-toggle"
        :class="{ on: historyOpen }"
        @click="historyOpen = !historyOpen"
      >
        <span class="ico">◷</span>
        最近提问
        <span v-if="conversations.length" class="badge">{{ conversations.length }}</span>
      </button>
    </header>

    <main ref="scroller" class="chat-main">
        <div class="thread">
          <!-- 首次进入：欢迎区 -->
          <div v-if="firstVisit" class="hello">
            <span class="hello-mark">知</span>
            <h3>校园政策，问一句就够</h3>
            <p class="muted small">
              从奖学金条件到成绩单办理，回答只依据知识库里的政策原文。
            </p>
            <ul class="hello-points">
              <li>每个 [n] 对应一条真实片段，可展开看原文</li>
              <li>同一政策有多个版本时并列提示，不替你挑一个</li>
              <li>觉得不对，可以标记「没帮助」并选原因</li>
            </ul>
          </div>

          <!-- 这段对话里之前的轮次（本轮那两条不在其中） -->
          <template v-if="historyTurns.length">
            <div v-if="!hasResult && !asking" class="resumed-note muted tiny">
              以下是这次对话里之前的问答；要继续追问可以直接往下问，想重新开始点「新对话」。
            </div>
            <template v-for="(m, i) in historyTurns" :key="`${m.timestamp}-${i}`">
              <div v-if="m.role === 'user'" class="turn-user past">
                <p>{{ m.content }}</p>
              </div>
              <section v-else class="turn-bot past">
                <div class="bot-head">
                  <span class="kicker">
                    {{ m.kind === 'refusal' ? '未作答：证据不足'
                      : m.kind === 'degraded' ? '原文片断'
                      : m.kind === 'citation_rejected' ? '已拦下'
                      : m.kind === 'fatal' ? '请求失败' : '解答' }}
                  </span>
                  <span class="muted tiny">{{ formatTime(m.timestamp) }}</span>
                </div>
                <div v-if="m.kind === 'answer'" class="answer-text" v-html="renderText(m.content)" />
                <p v-else class="muted small pre">{{ m.content }}</p>
                <ul v-if="m.citations?.length" class="cite-list compact">
                  <li v-for="c in m.citations" :key="`${i}-${c.index}`">
                    <button
                      class="cite-head"
                      @click="openPast = openPast === `${i}-${c.index}` ? '' : `${i}-${c.index}`"
                    >
                      <span class="tag plain">[{{ c.index }}]</span>
                      <strong>{{ c.document_title }}</strong>
                      <span class="muted small">{{ evidenceLabel(c.page_number, c.section_title) }}</span>
                      <span class="cite-toggle">
                        {{ openPast === `${i}-${c.index}` ? '收起' : '展开' }}
                      </span>
                    </button>
                    <!-- 缓存里只留了 160 字摘要，展开看的是这段摘要而不是全文；
                         要看全文得回原问题重新检索——这一点在提示里写明，别让人以为被截断了 -->
                    <p v-if="openPast === `${i}-${c.index}`" class="cite-body">
                      {{ c.snippet || '（本地缓存未保留这段原文，请重新提问以查看完整片段）' }}
                    </p>
                  </li>
                </ul>
              </section>
            </template>
            <!-- 上一问没等到回答（请求还在飞时就切走了，或者页面被关掉）：
                 如实说一句，别让用户对着一句孤零零的提问猜"系统是不是没理我" -->
            <p v-if="historyTurns[historyTurns.length - 1].role === 'user'"
               class="muted tiny pending-note">
              这一问没有等到回答（切换页面或关闭时请求被中断），可以重新问一次。
            </p>
          </template>

          <!-- 这一轮问的问题 -->
          <div v-if="askedQuestion && !firstVisit" class="turn-user">
            <p>{{ askedQuestion }}</p>
          </div>

          <!-- 推理中 -->
          <div v-if="asking" class="turn-bot thinking">
            <span class="spin" />
            <span class="muted small">
              正在检索政策原文并组织解答… {{ (elapsed / 1000).toFixed(1) }}s
            </span>
          </div>

          <!-- ============ 成功：解答 ============ -->
          <section v-if="answer" class="turn-bot">
            <div class="bot-head">
              <span class="kicker">解答</span>
              <span class="tag met">把握度 {{ confidenceLabel[answer.confidence] ?? answer.confidence }}</span>
              <span class="muted tiny">
                事项：{{ answer.matter.label }} ·
                用时 {{ (answer.total_ms / 1000).toFixed(1) }} 秒 ·
                引用 {{ answer.citations.length }} 条
                <template v-if="answer.history_pairs"> · 含 {{ answer.history_pairs }} 轮上文</template>
              </span>
            </div>

            <div class="answer-text" v-html="answerHtml" />
            <p v-if="answer.disclaimer" class="muted tiny">{{ answer.disclaimer }}</p>

            <h4 class="sec-title">引用片段</h4>
            <ol class="cite-list">
              <li v-for="c in answer.citations" :key="c.chunk_id">
                <button
                  class="cite-head"
                  @click="openCitation = openCitation === c.chunk_id ? '' : c.chunk_id"
                >
                  <span class="tag plain">[{{ c.index }}]</span>
                  <strong>{{ c.document_title }}</strong>
                  <span class="muted small">{{ evidenceLabel(c.page_number, c.section_title) }}</span>
                  <span class="muted tiny mono">覆盖率 {{ (c.coverage * 100).toFixed(0) }}%</span>
                  <span v-if="c.injection" class="tag unknown" title="该片段含指令式文字，已降权">含疑点</span>
                  <span class="cite-toggle">{{ openCitation === c.chunk_id ? '收起' : '展开' }}</span>
                </button>
                <p v-if="openCitation === c.chunk_id" class="cite-body">{{ c.content }}</p>
              </li>
            </ol>

            <!-- 政策原文：只给站内链接，不伪造外部网址 -->
            <template v-if="sources.length">
              <h4 class="sec-title">政策原文</h4>
              <ul class="source-list">
                <li v-for="s in sources" :key="s.id">
                  <span class="src-name">{{ s.title }}</span>
                  <router-link class="src-link" :to="{ path: '/library', query: { doc: s.id } }">
                    在知识库中打开 →
                  </router-link>
                </li>
              </ul>
              <p class="muted tiny">
                链接指向本站知识库中的文件（均为虚构示例，不指向任何真实机构）。
                系统不提供、也不编造外部网站地址。
              </p>
            </template>

            <div v-if="answer.citation_warnings" class="sub-block">
              <p class="muted small">
                越界编号 {{ answer.citation_warnings.rejected_indexes.join('、') }} 已剔除；
                保留 {{ answer.citation_warnings.valid_indexes.length }} 条有效引用。
              </p>
            </div>

            <!-- 反馈 -->
            <div class="feedback">
              <span class="muted small">这条解答有用吗？</span>
              <template v-if="feedbackOf(answer.message_id)">
                <span class="tag" :class="feedbackOf(answer.message_id)?.helpful ? 'met' : 'not_met'">
                  {{ feedbackOf(answer.message_id)?.helpful ? '已标记有用' : '已标记没帮助' }}
                  <template v-if="feedbackOf(answer.message_id)?.reason_code">
                    · {{ FEEDBACK_REASON_LABEL[feedbackOf(answer.message_id)?.reason_code ?? ''] ?? '' }}
                  </template>
                </span>
              </template>
              <template v-else>
                <button class="chip tiny-chip" :disabled="feedbackBusy" @click="submitFeedback(answer.message_id, true)">
                  有用
                </button>
                <button
                  class="chip tiny-chip"
                  :disabled="feedbackBusy"
                  @click="ratingFor = ratingFor === answer.message_id ? '' : answer.message_id"
                >
                  没帮助
                </button>
              </template>
            </div>
            <div v-if="ratingFor === answer.message_id" class="feedback-form">
              <select v-model="ratingReason" class="select">
                <option value="">选择原因（必填）</option>
                <option v-for="reason in reasons" :key="reason" :value="reason">
                  {{ FEEDBACK_REASON_LABEL[reason] }}
                </option>
              </select>
              <button class="btn tiny-btn" :disabled="feedbackBusy || !ratingReason" @click="submitFeedback(answer.message_id, false)">
                提交
              </button>
            </div>
            <p v-if="feedbackError" class="muted tiny">{{ feedbackError }}</p>

            <!-- 原「下一步」的动作入口：降级为一行小字，不再占一个板块 -->
            <p v-if="answer.next_actions.length" class="related">
              相关操作：
              <template v-for="(action, i) in answer.next_actions" :key="action.action">
                <span v-if="i > 0" class="muted tiny"> · </span>
                <button class="link" @click="goAction(action.action)">{{ action.label }}</button>
              </template>
            </p>
          </section>

          <!-- ============ 节点轨迹（可折叠） ============ -->
          <details v-if="workflow" class="trace-box">
            <summary>
              <span>本次处理过程</span>
              <span class="muted tiny">
                {{ workflow.executed_count }} / {{ workflow.node_count }} 个节点执行 ·
                共 {{ workflow.total_ms }} ms · 模型调用 {{ workflow.model_calls }} 次
                <template v-if="workflow.outcome"> · {{ RUN_OUTCOME_LABEL[workflow.outcome] ?? workflow.outcome }}</template>
              </span>
            </summary>
            <ol class="trace">
              <li
                v-for="node in workflow.nodes"
                :key="node.node"
                :class="['trace-node', nodeStatus(node.status).tone]"
                :title="node.summary"
              >
                <span class="trace-label">{{ node.label }}</span>
                <span class="trace-status">{{ nodeStatus(node.status).label }}</span>
                <span class="trace-meta mono">
                  <template v-if="node.status === 'skipped'">—</template>
                  <template v-else>{{ node.ms }} ms</template>
                </span>
              </li>
            </ol>
            <ul class="trace-summaries">
              <li v-for="node in workflow.nodes" :key="node.node">
                <strong>{{ node.label }}</strong>
                <span class="tag plain">{{ nodeStatus(node.status).label }}</span>
                <span class="muted small">{{ node.summary }}</span>
              </li>
            </ul>
            <p class="muted tiny">
              系统按「理解问题 → 检索原文 → 组织解答 → 校验引用」四步处理这次提问；
              灰显的步骤本次没有涉及（资格匹配与计划生成属于另外两条链路）。
            </p>
          </details>

          <!-- ============ 版本冲突（T-12） ============ -->
          <section v-if="shownConflicts.length" class="bot-warn">
            <div class="warn-head">
              <span class="kicker warn">同一政策存在多个版本</span>
              <span class="tag unknown">请核对后再采信</span>
            </div>
            <div v-for="conflict in shownConflicts" :key="conflict.topic" class="conflict">
              <p class="muted small">{{ conflict.hint }}</p>
              <table class="conflict-table">
                <thead>
                  <tr>
                    <th>文件</th>
                    <th>年份</th>
                    <th>发布日期</th>
                    <th>有效状态</th>
                    <th>本次引用</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="doc in conflict.documents"
                    :key="doc.document_id"
                    :class="{ newest: doc.document_id === conflict.newest_document_id }"
                  >
                    <td>
                      {{ doc.title }}
                      <span v-if="doc.document_id === conflict.newest_document_id" class="tag met">较新</span>
                    </td>
                    <td class="mono">{{ doc.year ?? '未标注' }}</td>
                    <td class="mono">{{ doc.published_at ?? '未标注' }}</td>
                    <td>{{ EFFECTIVE_LABEL[doc.effective_status] ?? doc.effective_status }}</td>
                    <td class="mono">{{ conflict.cited_indexes.length ? '已引用' : '未引用' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <!-- ============ 疑似注入（T-11） ============ -->
          <section v-if="injections.length" class="bot-warn">
            <div class="warn-head">
              <span class="kicker warn">片段里含有形似指令的文字</span>
              <span class="tag unknown">{{ injections.length }} 条</span>
            </div>
            <p class="muted small">
              这些片段被降权处理（覆盖率 ×0.5）。系统只把它们当作待引用的政策正文，
              不会执行其中的任何要求。
            </p>
            <ul class="reject-list">
              <li v-for="item in injections" :key="item.chunk_id">
                <strong class="small">{{ item.document_title }}</strong>
                <span class="tag plain mono">{{ item.patterns.join(' / ') }}</span>
                <span class="muted tiny mono">
                  覆盖率 {{ (item.original_coverage * 100).toFixed(0) }}%
                  → {{ (item.adjusted_coverage * 100).toFixed(0) }}%
                </span>
                <span :class="['tag', item.kept ? 'unknown' : 'plain']">
                  {{ item.kept ? '仍在证据中（已标记）' : '已低于阈值，未进入证据' }}
                </span>
              </li>
            </ul>
          </section>

          <!-- ============ 拒答：证据不足 ============ -->
          <section v-if="refusal" class="turn-bot warn">
            <div class="bot-head">
              <span class="kicker warn">无法作答：证据不足</span>
              <span class="tag unknown">4001</span>
            </div>
            <p class="muted">{{ refusal.reason }}</p>
            <p class="muted small">
              检索范围：{{ refusal.retrieval.index_size }} 个片段，命中 {{ refusal.retrieval.total_hits }} 条，
              覆盖率阈值 {{ (refusal.retrieval.min_coverage * 100).toFixed(0) }}%。
            </p>

            <div v-if="refusal.rejected_candidates.length" class="sub-block">
              <h4 class="sec-title">被覆盖率闸门拦下的片段</h4>
              <p class="muted tiny">
                这些片段被判定为"和你问的不是一件事"。如果你觉得其中某条就是你要的，可以换用政策原文里的措辞再问。
              </p>
              <ul class="reject-list">
                <li v-for="c in refusal.rejected_candidates" :key="c.chunk_id">
                  <span class="muted small">{{ c.document_title }}</span>
                  <span class="muted tiny mono">覆盖率 {{ (c.coverage * 100).toFixed(0) }}%</span>
                  <p class="cite-body">{{ c.content }}</p>
                </li>
              </ul>
            </div>

            <p class="muted tiny">
              换个问法可以直接点下方建议，或到「政策知识库」翻原文。
            </p>
          </section>

          <!-- ============ 降级：未配模型 ============ -->
          <section v-if="degraded" class="turn-bot warn">
            <div class="bot-head">
              <span class="kicker warn">降级模式：未配置模型</span>
              <span class="tag unknown">4006</span>
            </div>
            <p class="muted">{{ degraded.note }}</p>
            <ul class="reject-list">
              <li v-for="c in degraded.candidates" :key="c.chunk_id">
                <span class="muted small">{{ c.document_title }}</span>
                <p class="cite-body">{{ c.content }}</p>
              </li>
            </ul>
          </section>

          <!-- ============ 引用越界被拒 ============ -->
          <section v-if="citationRejected" class="turn-bot warn">
            <div class="bot-head">
              <span class="kicker warn">这条解答被拦下：引用对不上原文</span>
              <span class="tag not_met">4003</span>
            </div>
            <p class="muted">
              模型给出的内容里有 {{ citationRejected.rejected_indexes.length }} 处引用编号越界
              （合法区间 1–{{ citationRejected.valid_indexes.length }}）。
              无法核验的内容一律不返回——这比返回一个猜的结论安全。
            </p>
            <div class="sub-block">
              <h4 class="sec-title">被丢弃的草稿</h4>
              <p class="draft">{{ citationRejected.answer_draft }}</p>
            </div>
          </section>

          <!-- ============ 真故障 ============ -->
          <section v-if="fatal" class="turn-bot err">
            <h3>请求失败</h3>
            <p class="muted">{{ fatal }}</p>
            <p class="muted tiny">
              如果是 4002，说明模型这次没能在预算内给出结果（不是"没有依据"），可以重试或换个问法。
            </p>
          </section>
        </div>
      </main>

      <!-- 右侧：我提问过的问题 -->
      <aside v-show="historyOpen" class="chat-side">
        <div class="side-head">
          <span>我提问过的问题</span>
          <span class="muted tiny">{{ conversations.length }}</span>
        </div>
        <p v-if="!conversations.length" class="muted tiny side-note">
          还没有提问记录。问第一个问题后，这里会留下记录。
        </p>
        <ul v-else class="history-list">
          <li v-for="c in conversations" :key="c.id">
            <button
              :class="{ on: question === c.title }"
              :title="c.title"
              @click="fillFromHistory(c)"
            >
              <span class="h-title">{{ c.title }}</span>
              <span class="h-time">{{ formatTime(c.created_at) }}</span>
            </button>
          </li>
        </ul>
        <p v-if="conversations.length" class="muted tiny side-note">
          点任一条会填入下方输入框，再点「发送」才会重新解答。
        </p>
    </aside>

    <!-- 底部：建议问题 + 输入框（固定，不随内容滚动） -->
    <footer class="composer">
      <div v-if="suggestOpen" class="suggest">
        <span class="suggest-label">{{ suggestTitle }}</span>
        <div class="chips">
          <button
            v-for="s in suggestItems"
            :key="s"
            class="chip"
            @mousedown.prevent
            @click="useSuggestion(s)"
          >{{ s }}</button>
        </div>
      </div>
      <div class="composer-box">
        <textarea
          ref="inputRef"
          v-model="question"
          class="composer-input"
          rows="1"
          placeholder="输入你的问题，例如：申请国家励志奖学金需要满足哪些条件？"
          @focus="focusOpen = true"
          @blur="focusOpen = false"
          @input="autoGrow"
          @keydown.ctrl.enter.prevent="ask()"
        />
        <button class="send" :disabled="asking || !question.trim()" @click="ask()">
          {{ asking ? '处理中' : '发送' }}
        </button>
      </div>
      <div class="composer-foot">
        <span>Ctrl + Enter 发送</span>
        <span>回答只依据知识库中的政策原文</span>
      </div>
    </footer>
  </div>
</template>

<style scoped>
/* 整个聊天区固定成一屏：内容区自己滚，输入框永远在最下方 */
/* 整个聊天区固定成一屏：内容区自己滚，输入框永远在最下方。
   用三行网格而不是 flex 列，是因为输入框要和左侧解答区**同宽对齐**——
   flex 列会让输入框横跨整页（含右侧历史栏），看起来像浮在右边而不是接在对话下面。 */
.chat {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 244px;
  grid-template-rows: auto minmax(0, 1fr) auto;
  column-gap: var(--space-4);
  /* 内容区自身有 16px 上内边距和 32px 下内边距，聊天页要把下内边距吃回去：
     height 扣掉顶栏与上内边距，再用负 margin 抵消下内边距，
     总高正好一屏——否则输入框会离窗口底 32px，看着像没贴住。 */
  height: calc(100vh - 56px - var(--space-4));
  margin-bottom: calc(var(--space-8) * -1);
  min-height: 480px;
}

.chat.no-side {
  grid-template-columns: minmax(0, 1fr);
}

.chat-head {
  grid-column: 1 / -1;
  grid-row: 1;
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding-bottom: var(--space-3);
  flex-wrap: wrap;
}

.chat-title h2 {
  margin: 0 0 3px;
  font-size: 17px;
}

.chat-title p {
  margin: 0;
  max-width: 720px;
}

.small {
  font-size: 12.5px;
}

.tiny {
  font-size: 11.5px;
}

.history-toggle {
  margin-left: auto;
  gap: 6px;
}

.history-toggle.on {
  border-color: var(--c-primary);
  color: var(--c-primary);
}

.history-toggle .ico {
  opacity: 0.8;
}

.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--c-primary-weak);
  color: var(--c-primary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.chat-main {
  grid-column: 1;
  grid-row: 2;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding-right: 6px;
}

/* 对话列限宽：一行 90 个汉字读起来最舒服，铺满整屏反而难读 */
.thread {
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding-bottom: var(--space-4);
}

/* ---------- 欢迎区 ---------- */
.hello {
  padding: var(--space-8) var(--space-4) var(--space-4);
  text-align: center;
}

.hello-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: 14px;
  background: var(--c-primary);
  color: #fff;
  font-size: 22px;
  font-weight: 700;
  margin-bottom: var(--space-3);
}

.hello h3 {
  margin: 0 0 var(--space-2);
  font-size: 18px;
}

.hello-points {
  list-style: none;
  margin: var(--space-4) auto 0;
  padding: 0;
  max-width: 460px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  text-align: left;
  font-size: 12.5px;
  color: var(--c-text-2);
}

.hello-points li::before {
  content: "·";
  margin-right: 8px;
  color: var(--c-primary);
}

/* ---------- 气泡 ---------- */
.turn-user {
  align-self: flex-end;
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 14px 14px 4px 14px;
  background: var(--c-primary);
  color: #fff;
}

.turn-user p {
  margin: 0;
  font-size: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
}

.turn-bot {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 4px 14px 14px 14px;
  padding: var(--space-4);
}

.turn-bot.warn {
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.turn-bot.err {
  border-color: var(--c-not-met-border);
  background: var(--c-not-met-bg);
}

.thinking {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

/* ---------- 切页回来铺出的历史轮次 ---------- */
/* 刻意降一档对比度：它们是"上文"，当前这一轮才是用户要读的。
   但不做灰到看不清——用户回来就是为了重读它。 */
.resumed-note {
  padding: 2px 0 6px;
  border-bottom: 1px dashed var(--c-border);
  margin-bottom: 4px;
}

.turn-user.past {
  opacity: 0.78;
}

.turn-bot.past {
  background: transparent;
  border-style: dashed;
}

.turn-bot.past .answer-text {
  opacity: 0.86;
}

.pre {
  white-space: pre-wrap;
}

.cite-list.compact {
  margin-top: 6px;
}

.cite-head.static {
  cursor: default;
}

.cite-head.static:hover {
  background: transparent;
}

.new-chat {
  gap: 6px;
}

/* 未完成的一轮：说明文字紧跟在那句提问下面，不额外加边框免得像另一个回答 */
.pending-note {
  margin: -2px 0 6px;
  padding-left: 2px;
}

.spin {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid var(--c-primary-weak);
  border-top-color: var(--c-primary);
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.bot-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-bottom: var(--space-3);
}

.kicker {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 9px;
  border-radius: 6px;
  background: var(--c-primary-weak);
  color: var(--c-primary);
  letter-spacing: 0.4px;
}

.kicker.warn {
  background: var(--c-warn-bg);
  color: var(--c-unknown);
}

.bot-head .muted {
  margin-left: auto;
}

.answer-text {
  margin: 0 0 var(--space-3);
  white-space: pre-wrap;
  line-height: 1.9;
  font-size: 14.5px;
}

.sec-title {
  margin: var(--space-4) 0 var(--space-2);
  font-size: 13px;
  color: var(--c-text-2);
  font-weight: 600;
}

.sub-block {
  margin-top: var(--space-4);
}

/* ---------- 引用 ---------- */
.cite-list,
.reject-list,
.source-list,
.trace-summaries {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.cite-head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
  width: 100%;
  text-align: left;
  background: var(--c-bg);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  font-family: inherit;
  font-size: 13px;
  color: var(--c-text);
}

.cite-head:hover {
  border-color: var(--c-border-strong);
}

.cite-toggle {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--c-primary);
}

.cite-body {
  margin: var(--space-2) 0 0;
  padding: var(--space-3);
  background: var(--c-bg);
  border-left: 3px solid var(--c-primary-weak);
  border-radius: 0 var(--radius-control) var(--radius-control) 0;
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.8;
  color: var(--c-text-2);
}

/* ---------- 政策原文链接 ---------- */
.source-list li {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 8px 12px;
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-bg);
  font-size: 13px;
}

.src-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.src-link {
  margin-left: auto;
  white-space: nowrap;
  font-size: 12.5px;
}

/* ---------- 反馈 ---------- */
.feedback {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-4);
  flex-wrap: wrap;
}

.feedback-form {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
}

.select {
  padding: 5px 8px;
  border: 1px solid var(--c-border-strong);
  border-radius: var(--radius-control);
  font-family: inherit;
  font-size: 12.5px;
  color: var(--c-text);
  background: var(--c-surface-2);
}

.tiny-btn {
  min-height: 30px;
  padding: 0 10px;
  font-size: 12px;
}

.chip {
  padding: 6px 12px;
  border: 1px solid var(--c-border-strong);
  border-radius: 999px;
  background: var(--c-surface-2);
  color: var(--c-text);
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease, color 0.15s ease;
}

.chip:hover {
  border-color: var(--c-primary);
  color: var(--c-primary);
  background: var(--c-primary-weak);
}

.tiny-chip {
  padding: 3px 10px;
  font-size: 12px;
}

.related {
  margin: var(--space-4) 0 0;
  font-size: 12.5px;
  color: var(--c-text-2);
}

/* ---------- 轨迹 ---------- */
.trace-box {
  border: 1px solid var(--c-border);
  border-radius: var(--radius-card);
  background: var(--c-surface);
  padding: var(--space-3) var(--space-4);
}

.trace-box summary {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  cursor: pointer;
  font-size: 13px;
  flex-wrap: wrap;
}

.trace {
  list-style: none;
  margin: var(--space-3) 0 var(--space-2);
  padding: 0;
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--space-2);
}

.trace-node {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-bg);
  border-top-width: 3px;
}

.trace-node.met {
  border-top-color: var(--c-met);
}

.trace-node.unknown {
  border-top-color: var(--c-unknown);
}

.trace-node.not_met {
  border-top-color: var(--c-not-met);
}

.trace-node.plain {
  border-top-color: var(--c-border);
  opacity: 0.55;
}

.trace-label {
  font-size: 12px;
  font-weight: 600;
}

.trace-status {
  font-size: 11px;
  color: var(--c-text-2);
}

.trace-meta {
  font-size: 10px;
  color: var(--c-text-2);
}

.trace-summaries {
  margin-bottom: var(--space-2);
  font-size: 12px;
}

.trace-summaries li {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
  flex-wrap: wrap;
}

/* ---------- 冲突 ---------- */
.bot-warn {
  border: 1px solid var(--c-warn-border);
  background: var(--c-warn-bg);
  border-radius: var(--radius-card);
  padding: var(--space-4);
}

.warn-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
}

.conflict + .conflict {
  margin-top: var(--space-4);
}

.conflict-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: var(--space-2);
  font-size: 12px;
}

.conflict-table th,
.conflict-table td {
  border: 1px solid var(--c-border);
  padding: 6px 8px;
  text-align: left;
  vertical-align: top;
}

.conflict-table th {
  background: var(--c-surface);
  font-weight: 600;
}

.conflict-table tr.newest td {
  background: var(--c-met-bg);
}

/* ---------- 拒答/降级的候选列表 ---------- */
.reject-list li {
  padding: var(--space-2) var(--space-3);
  border: 1px dashed var(--c-border);
  border-radius: var(--radius-control);
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
  flex-wrap: wrap;
}

.draft {
  margin: var(--space-2) 0 0;
  padding: var(--space-3);
  border-left: 3px solid var(--c-not-met);
  color: var(--c-text-2);
  white-space: pre-wrap;
  font-size: 13px;
}

/* ---------- 右侧历史 ---------- */
.chat-side {
  grid-column: 2;
  grid-row: 2 / 4;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  border-left: 1px solid var(--c-border);
  padding-left: var(--space-4);
  overflow-y: auto;
}

.side-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-size: 12.5px;
  color: var(--c-text-2);
  padding-bottom: 4px;
}

.side-note {
  margin: 0;
  line-height: 1.7;
}

.history-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.history-list button {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  text-align: left;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-control);
  background: none;
  color: var(--c-text);
  font-family: inherit;
  cursor: pointer;
}

.history-list button:hover {
  background: var(--c-surface-2);
}

.history-list button.on {
  background: var(--c-primary-weak);
  border-color: var(--c-primary-border);
}

.h-title {
  font-size: 12.5px;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.h-time {
  font-size: 11px;
  color: var(--c-dim);
}

/* ---------- 底部输入区 ---------- */
.composer {
  grid-column: 1;
  grid-row: 3;
  min-width: 0;
  padding: var(--space-3) 0 10px;
  border-top: 1px solid var(--c-border);
  background: var(--c-bg);
}

.suggest {
  margin: 0 0 var(--space-2);
  padding: 10px 12px;
  border: 1px solid var(--c-border);
  border-radius: var(--radius-card);
  background: var(--c-surface);
}

.suggest-label {
  display: block;
  font-size: 11.5px;
  color: var(--c-text-2);
  margin-bottom: 6px;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.composer-box {
  margin: 0;
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
  padding: 6px 6px 6px 12px;
  border: 1px solid var(--c-border-strong);
  border-radius: 16px;
  background: var(--c-surface);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.composer-box:focus-within {
  border-color: var(--c-primary);
  box-shadow: 0 0 0 3px var(--c-primary-weak);
}

.composer-input {
  flex: 1;
  min-height: 34px;
  max-height: 132px;
  padding: 6px 0;
  border: none;
  background: none;
  color: var(--c-text);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.7;
  resize: none;
  outline: none;
}

.composer-input::placeholder {
  color: var(--c-dim);
}

.send {
  flex: none;
  min-height: 34px;
  padding: 0 18px;
  border: 1px solid var(--c-primary);
  border-radius: 12px;
  background: var(--c-primary);
  color: #fff;
  font-family: inherit;
  font-size: 13.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s ease;
}

.send:hover:not(:disabled) {
  background: #3b6fd4;
}

.send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.composer-foot {
  margin: 5px 0 0;
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  font-size: 11px;
  color: var(--c-dim);
}

/* ---------- 通用 ---------- */
.tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11.5px;
  border: 1px solid var(--c-border);
  white-space: nowrap;
}

.tag.met {
  color: var(--c-met);
  border-color: var(--c-met-border);
  background: var(--c-met-bg);
}

.tag.unknown {
  color: var(--c-unknown);
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.tag.not_met {
  color: var(--c-not-met);
  border-color: var(--c-not-met-border);
  background: var(--c-not-met-bg);
}

.tag.plain {
  color: var(--c-text-2);
  background: var(--c-surface-2);
}

@media (max-width: 1080px) {
  .chat {
    grid-template-columns: minmax(0, 1fr);
  }

  .chat-side {
    display: none;
  }
}

@media (max-width: 800px) {
  .trace {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
</style>
