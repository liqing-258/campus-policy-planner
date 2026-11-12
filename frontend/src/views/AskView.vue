<script setup lang="ts">
/**
 * 智能问答（M4 建、M6 补全、M7 后改为对话式布局）。
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
 * 4001/4003/4006 都是 HTTP 200 的业务态，所以用 api.raw 自己按 code 分支，
 * 不能像普通接口那样 try/catch 当错误处理。
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
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

const router = useRouter()

const question = ref('')
/** 这一轮实际问出去的问题。单独存是因为输入框随时会被改成下一条 */
const askedQuestion = ref('')
const asking = ref(false)
const elapsed = ref(0)

const answer = ref<AnswerData | null>(null)
const refusal = ref<RefusalData | null>(null)
const degraded = ref<DegradedData | null>(null)
const citationRejected = ref<CitationRejectedData | null>(null)
const fatal = ref('')
const conversationId = ref('')
/**
 * 当前会话的首轮问题。后端 `/qa/ask` 认 `conversation_id`：带上它就把这一问续进
 * 那个会话（并把最近几轮拼进 prompt），不带就新建一条。
 * 记标题是为了判断"是不是原样再问一遍"——见 ask()。
 */
const conversationTitle = ref('')

const openCitation = ref('')

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
  answer.value = null
  refusal.value = null
  degraded.value = null
  citationRejected.value = null
  fatal.value = ''
  openCitation.value = ''
  ratingFor.value = ''
  ratingReason.value = ''
  feedbackError.value = ''
  const timer = window.setInterval(() => (elapsed.value += 100), 100)

  /**
   * 点历史问题后原样再问一遍 → 带上那个会话的 id，续在原会话里。
   *
   * 不带的话后端每问一次就新建一条会话，右侧"我提问过的问题"里立刻多出一行
   * 和刚才一模一样的记录——用户会以为点按钮点出了 bug。
   * 只在问题文本没被改过时才续：用户把它改成另一件事，就该是新的一个问题。
   */
  const reuse = conversationId.value && payload === conversationTitle.value
  const body: Record<string, unknown> = { question: payload }
  if (reuse) body.conversation_id = conversationId.value

  try {
    const envelope = await api.raw<unknown>('/qa/ask', body)
    if (envelope.code === 0) {
      answer.value = envelope.data as AnswerData
      conversationId.value = answer.value.conversation_id
    } else if (envelope.code === 4001) {
      refusal.value = envelope.data as RefusalData
      conversationId.value = refusal.value.conversation_id ?? ''
    } else if (envelope.code === 4006) {
      degraded.value = envelope.data as DegradedData
      conversationId.value = degraded.value.conversation_id ?? ''
    } else if (envelope.code === 4003) {
      citationRejected.value = envelope.data as CitationRejectedData
      conversationId.value = ''
    } else {
      fatal.value = `${envelope.code} · ${envelope.message}`
    }
    conversationTitle.value = payload
  } catch (err) {
    // 4002/4004/4005 是真故障，必须和"没找到依据"区分开
    fatal.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '请求失败')
  } finally {
    window.clearInterval(timer)
    asking.value = false
    await loadConversations()
    await nextTick()
    scroller.value?.scrollTo({ top: 0, behavior: 'smooth' })
  }
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
 */
function fillFromHistory(row: ConversationRow) {
  question.value = row.title
  conversationId.value = row.id
  conversationTitle.value = row.title
  focusOpen.value = true
  nextTick(() => {
    inputRef.value?.focus()
    autoGrow()
  })
}

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

/** 一次解答都没有、也没在问——显示欢迎区，而不是一片空白 */
const firstVisit = computed(() => !hasResult.value && !asking.value && !askedQuestion.value)

/** 先把 HTML 转义再加粗——模型输出不可信，不能直接塞进 v-html */
function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

/** 只处理 **粗体**：模型爱用它标条件名，原样显示会看到一堆星号 */
const answerHtml = computed(() =>
  escapeHtml(answer.value?.answer ?? '').replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>'),
)

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
  /**
   * 「其他事项」是意图分类没认出具体名目时的占位说法。
   * 拿它造句会得到"其他事项需要准备哪些材料？"——读着像废话，点下去也答不出具体东西。
   * 这时把主语换成引用到的文件名，至少让追问落在一份具体规定上。
   */
  const vague = /^其他/.test(label)
  const out: string[] = []
  if (relatedConflict.value) {
    out.push(`新旧两版《${relatedConflict.value.topic}》规定不一致时，以哪一版为准？`)
  }
  if (refusal.value) {
    out.push(...refusal.value.suggestions)
  } else if (vague && doc) {
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

onMounted(loadConversations)
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
