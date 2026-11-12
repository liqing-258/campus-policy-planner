<script setup lang="ts">
/**
 * 智能问答（M4 建、M6 补全）。
 *
 * 这一页要能同时演示五件事，缺一件就不算"可信"：
 * 1. 答得出来 —— 正文 + 每条结论后面的 [n]，点开能看到原文
 * 2. 答不出来就拒答 —— 明确说"证据不足"，并列出被闸门拦下的候选
 * 3. 未配模型时降级 —— 只给原文片段，不假装给过归纳
 * 4. **节点轨迹是真的** —— 执行过几个节点就显示几个"完成"，
 *    没执行的（matching / planning）显示灰的"未执行"并给原因
 * 5. **可疑内容必须显形** —— 版本冲突并列展示、疑似注入的片段带标记
 *
 * 4001/4003/4006 都是 HTTP 200 的业务态，所以用 api.raw 自己按 code 分支，
 * 不能像普通接口那样 try/catch 当错误处理。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ApiError,
  api,
  type AnswerData,
  type CitationRejectedData,
  type ConversationRow,
  type DegradedData,
  type FeedbackReason,
  type MessageRow,
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
const asking = ref(false)
const elapsed = ref(0)

const answer = ref<AnswerData | null>(null)
const refusal = ref<RefusalData | null>(null)
const degraded = ref<DegradedData | null>(null)
const citationRejected = ref<CitationRejectedData | null>(null)
const fatal = ref('')
const conversationId = ref('')

const openCitation = ref('')

const conversations = ref<ConversationRow[]>([])
const activeConversation = ref('')
const messages = ref<MessageRow[]>([])

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
  return feedbackSent.value[messageId] ?? answerMessageFeedback.value[messageId] ?? null
}

/** 历史里带回来的反馈（刷新后不丢） */
const answerMessageFeedback = computed<Record<string, { helpful: boolean; reason_code: string | null }>>(() => {
  const map: Record<string, { helpful: boolean; reason_code: string | null }> = {}
  for (const message of messages.value) {
    if (message.feedback) map[message.id] = message.feedback
  }
  return map
})

async function ask() {
  const text = question.value.trim()
  if (!text || asking.value) return
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

  try {
    const envelope = await api.raw<unknown>('/qa/ask', { question: text })
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
  } catch (err) {
    // 4002/4004/4005 是真故障，必须和"没找到依据"区分开
    fatal.value = err instanceof ApiError
      ? `${err.code} · ${err.message}`
      : (err instanceof Error ? err.message : '请求失败')
  } finally {
    window.clearInterval(timer)
    asking.value = false
    await loadConversations()
  }
}

async function loadConversations() {
  try {
    const data = await api.get<Paged<ConversationRow>>('/conversations?page=1&page_size=8')
    conversations.value = data.items
  } catch {
    /* 历史列表失败不该盖住主结果，静默即可 */
  }
}

async function openHistory(row: ConversationRow) {
  if (activeConversation.value === row.id) {
    activeConversation.value = ''
    messages.value = []
    return
  }
  activeConversation.value = row.id
  messages.value = []
  try {
    const data = await api.get<Paged<MessageRow>>(
      `/conversations/${row.id}/messages?page=1&page_size=50`,
    )
    messages.value = data.items
  } catch (err) {
    fatal.value = err instanceof Error ? err.message : '历史加载失败'
  }
}

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

const empty = computed(() => !answer.value && !refusal.value && !degraded.value
  && !citationRejected.value && !fatal.value)

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

const examples = [
  '申请国家励志奖学金需要满足哪些条件？',
  '国家励志奖学金的申请截止时间是哪天？',
  '家庭经济困难认定需要提交哪些材料？',
]

function askExample(text: string) {
  question.value = text
  ask()
}

function goAction(action: string) {
  router.push(action === 'match' ? '/match' : '/plans')
}

onMounted(loadConversations)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>智能问答</h2>
        <p class="muted small">
          答案里的每个 [n] 都对应右侧一条真实片段。若证据不足，系统会拒答并列出被拦下的片段，
          而不是编一个看起来合理的答案。
        </p>
      </div>
    </header>

    <section class="card ask-card">
      <textarea
        v-model="question"
        class="ask-input"
        rows="3"
        placeholder="例如：申请国家励志奖学金需要满足哪些条件？"
        @keydown.ctrl.enter="ask"
      />
      <div class="ask-actions">
        <span class="muted tiny">Ctrl + Enter 提交</span>
        <button class="btn" :disabled="asking || !question.trim()" @click="ask">
          {{ asking ? `推理中… ${(elapsed / 1000).toFixed(1)}s` : '提问' }}
        </button>
      </div>
    </section>

    <!-- ============ 节点轨迹（M6）============ -->
    <section v-if="workflow" class="card trace-card">
      <div class="result-head">
        <h3>本次处理过程</h3>
        <span class="muted tiny">
          {{ workflow.executed_count }} / {{ workflow.node_count }} 个节点执行 ·
          共 {{ workflow.total_ms }} ms · 模型调用 {{ workflow.model_calls }} 次
          <template v-if="workflow.outcome"> · {{ RUN_OUTCOME_LABEL[workflow.outcome] ?? workflow.outcome }}</template>
        </span>
      </div>
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
      <p class="muted tiny">
        系统按「理解问题 → 检索原文 → 组织答案 → 校验引用」四步处理这次提问；
        灰显的步骤本次没有涉及（资格匹配与计划生成属于另外两条链路）。</p>
      <details class="trace-detail">
        <summary class="muted tiny">展开每个节点的摘要</summary>
        <ul class="trace-summaries">
          <li v-for="node in workflow.nodes" :key="node.node">
            <strong>{{ node.label }}</strong>
            <span class="tag plain">{{ nodeStatus(node.status).label }}</span>
            <span class="muted small">{{ node.summary }}</span>
            <span v-if="node.error" class="muted tiny mono"> · {{ node.error }}</span>
          </li>
        </ul>
      </details>
    </section>

    <!-- ============ 版本冲突（T-12）============ -->
    <section v-if="conflicts.length" class="card warn-card">
      <div class="result-head">
        <h3>同一政策存在多个版本</h3>
        <span class="tag unknown">请核对后再采信</span>
      </div>
      <div v-for="conflict in conflicts" :key="conflict.topic" class="conflict">
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
              <td class="mono">
                {{ conflict.cited_indexes.length ? '已引用' : '未引用' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ============ 疑似注入（T-11）============ -->
    <section v-if="injections.length" class="card warn-card">
      <div class="result-head">
        <h3>片段里含有形似指令的文字</h3>
        <span class="tag unknown">{{ injections.length }} 条</span>
      </div>
      <p class="muted small">
        这些片段被降权处理（覆盖率 ×0.5）。系统只把它们当作待引用的政策正文，
        不会执行其中的任何要求；命中记录如下。
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

    <!-- ============ 成功：答案 + 引用 ============ -->
    <section v-if="answer" class="card">
      <div class="result-head">
        <h3>答案</h3>
        <span class="tag met">把握度：{{ confidenceLabel[answer.confidence] ?? answer.confidence }}</span>
      </div>
      <p class="answer-text" v-html="answerHtml" />
      <p class="muted tiny">
        事项：{{ answer.matter.label }} ·
        用时 {{ (answer.total_ms / 1000).toFixed(1) }} 秒 ·
        引用 {{ answer.citations.length }} 条
        <template v-if="answer.history_pairs"> · 含 {{ answer.history_pairs }} 轮上文</template></p>
      <p v-if="answer.disclaimer" class="muted tiny">{{ answer.disclaimer }}</p>

      <!-- 反馈 -->
      <div class="feedback">
        <span class="muted small">这条回答有用吗？</span>
        <template v-if="feedbackOf(answer.message_id)">
          <span class="tag" :class="feedbackOf(answer.message_id)?.helpful ? 'met' : 'not_met'">
            {{ feedbackOf(answer.message_id)?.helpful ? '已标记有用' : '已标记没帮助' }}
            <template v-if="feedbackOf(answer.message_id)?.reason_code">
              · {{ FEEDBACK_REASON_LABEL[feedbackOf(answer.message_id)?.reason_code ?? ''] ?? '' }}
            </template>
          </span>
        </template>
        <template v-else>
          <button class="btn btn-ghost tiny-btn" :disabled="feedbackBusy" @click="submitFeedback(answer.message_id, true)">
            有用
          </button>
          <button
            class="btn btn-ghost tiny-btn"
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

      <h3 class="sub">引用片段</h3>
      <ol class="cite-list">
        <li v-for="c in answer.citations" :key="c.chunk_id">
          <button class="cite-head" @click="openCitation = openCitation === c.chunk_id ? '' : c.chunk_id">
            <span class="tag plain">[{{ c.index }}]</span>
            <strong>{{ c.document_title }}</strong>
            <span class="muted small">{{ evidenceLabel(c.page_number, c.section_title) }}</span>
            <span class="muted tiny mono">覆盖率 {{ (c.coverage * 100).toFixed(0) }}%</span>
            <span v-if="c.injection" class="tag unknown" title="该片段含指令式文字，已降权">含疑点</span>
          </button>
          <p v-if="openCitation === c.chunk_id" class="cite-body">{{ c.content }}</p>
        </li>
      </ol>

      <div v-if="answer.citation_warnings" class="sub-block">
        <h3 class="sub">引用告警</h3>
        <p class="muted small">
          越界编号 {{ answer.citation_warnings.rejected_indexes.join('、') }} 已剔除；
          保留 {{ answer.citation_warnings.valid_indexes.length }} 条有效引用。
        </p>
      </div>

      <div v-if="answer.next_actions.length" class="sub-block">
        <h3 class="sub">下一步</h3>
        <ul class="action-list">
          <li v-for="action in answer.next_actions" :key="action.action">
            <button class="btn btn-ghost tiny-btn" @click="goAction(action.action)">{{ action.label }}</button>
            <span class="muted tiny">{{ action.reason }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- 拒答：证据不足 -->
    <section v-if="refusal" class="card warn-card">
      <div class="result-head">
        <h3>无法作答：证据不足</h3>
        <span class="tag unknown">4001</span>
      </div>
      <p class="muted">{{ refusal.reason }}</p>
      <p class="muted small">
        检索范围：{{ refusal.retrieval.index_size }} 个片段，命中 {{ refusal.retrieval.total_hits }} 条，
        覆盖率阈值 {{ (refusal.retrieval.min_coverage * 100).toFixed(0) }}%。
      </p>

      <div v-if="refusal.rejected_candidates.length" class="sub-block">
        <h3 class="sub">被覆盖率闸门拦下的片段</h3>
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

      <div class="sub-block">
        <h3 class="sub">可以试试</h3>
        <ul class="suggest-list">
          <li v-for="s in refusal.suggestions" :key="s">{{ s }}</li>
        </ul>
      </div>
    </section>

    <!-- 降级：未配模型 -->
    <section v-if="degraded" class="card warn-card">
      <div class="result-head">
        <h3>降级模式：未配置模型</h3>
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

    <!-- 引用越界被拒 -->
    <section v-if="citationRejected" class="card warn-card">
      <div class="result-head">
        <h3>答案被拦下：引用对不上原文</h3>
        <span class="tag not_met">4003</span>
      </div>
      <p class="muted">
        模型给出的答案里有 {{ citationRejected.rejected_indexes.length }} 处引用编号越界
        （合法区间 1–{{ citationRejected.valid_indexes.length }}）。
        无法核验的答案一律不返回——这比返回一个猜的结论安全。
      </p>
      <div class="sub-block">
        <h3 class="sub">被丢弃的草稿</h3>
        <p class="draft">{{ citationRejected.answer_draft }}</p>
      </div>
    </section>

    <!-- 真故障 -->
    <section v-if="fatal" class="card err-card">
      <h3>请求失败</h3>
      <p class="muted">{{ fatal }}</p>
      <p class="muted tiny">
        如果是 4002，说明模型这次没能在预算内给出答案（不是"没有依据"），可以重试或换个问法。
      </p>
    </section>

    <section v-if="empty && !asking" class="card examples">
      <h3>可以这样问</h3>
      <ul>
        <li v-for="item in examples" :key="item">
          <button class="example" @click="askExample(item)">{{ item }}</button>
        </li>
      </ul>
      <p class="muted tiny">
        答案会标注编号引用，点开可以核对原文；证据不足时系统会明确拒答，而不是编一个答案。
      </p>
    </section>

    <!-- 历史 -->
    <section v-if="conversations.length" class="card">
      <h3>最近提问</h3>
      <ul class="history">
        <li v-for="c in conversations" :key="c.id">
          <button class="history-head" @click="openHistory(c)">
            <span>{{ c.title }}</span>
            <span class="muted tiny">{{ formatTime(c.created_at) }}</span>
          </button>
          <div v-if="activeConversation === c.id" class="bubbles">
            <div v-for="m in messages" :key="m.id" :class="['bubble', m.role]">
              <p>{{ m.content || '（该轮未生成答案）' }}</p>
              <div v-if="m.citations && m.citations.length" class="muted tiny">
                引用：<span v-for="ci in m.citations" :key="ci.chunk_id" class="mono">
                  [{{ ci.index }}] {{ ci.document_title }}
                </span>
              </div>
              <div v-if="m.role === 'assistant'" class="bubble-foot">
                <span v-if="m.workflow_run_id" class="muted tiny mono">
                  轨迹 {{ m.workflow_run_id.slice(-6) }}
                </span>
                <span v-if="m.feedback" class="tag" :class="m.feedback.helpful ? 'met' : 'not_met'">
                  {{ m.feedback.helpful ? '当时标记有用' : '当时标记没帮助' }}
                </span>
              </div>
            </div>
          </div>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
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

.ask-input {
  width: 100%;
  padding: var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.6;
  resize: vertical;
  color: var(--c-text);
}

.ask-input:focus {
  outline: 2px solid var(--c-primary-weak);
  border-color: var(--c-primary);
}

.ask-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: var(--space-3);
}

.result-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
}

.answer-text {
  margin: 0 0 var(--space-3);
  white-space: pre-wrap;
  line-height: 1.85;
}

.sub {
  margin: var(--space-4) 0 var(--space-2);
  font-size: 14px;
}

.sub-block {
  margin-top: var(--space-4);
}

/* ---------- 轨迹 ---------- */
.examples ul {
  list-style: none;
  margin: 0 0 var(--space-3);
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.example {
  width: 100%;
  text-align: left;
  padding: 9px 12px;
  border: 1px solid var(--c-border-strong);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
  color: var(--c-text);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.example:hover {
  border-color: var(--c-primary);
  background: var(--c-primary-weak);
  color: var(--c-primary);
}

.trace-card {
  background: var(--c-surface);
}

.trace {
  list-style: none;
  margin: 0 0 var(--space-2);
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

.trace-detail {
  margin-top: var(--space-2);
}

.trace-summaries {
  list-style: none;
  margin: var(--space-2) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}

.trace-summaries li {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
  flex-wrap: wrap;
}

/* ---------- 冲突 ---------- */
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

/* ---------- 反馈 ---------- */
.feedback {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-3);
  flex-wrap: wrap;
}

.feedback-form {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-2);
  align-items: center;
}

.select {
  padding: 4px 8px;
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  font-family: inherit;
  font-size: 12px;
  color: var(--c-text);
  background: var(--c-bg);
}

.tiny-btn {
  padding: 3px 10px;
  font-size: 12px;
}

.action-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.action-list li {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.cite-list,
.reject-list,
.suggest-list,
.history {
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

.cite-body {
  margin: var(--space-2) 0 0;
  padding: var(--space-3);
  background: var(--c-surface);
  border-left: 3px solid var(--c-primary-weak);
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.8;
  color: var(--c-text-2);
}

.reject-list li {
  padding: var(--space-2) var(--space-3);
  border: 1px dashed var(--c-border);
  border-radius: var(--radius-control);
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
  flex-wrap: wrap;
}

.suggest-list li {
  font-size: 13px;
  color: var(--c-text-2);
  padding-left: var(--space-4);
  position: relative;
}

.suggest-list li::before {
  content: "·";
  position: absolute;
  left: var(--space-2);
}

.warn-card {
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.err-card {
  border-color: var(--c-not-met-border);
  background: var(--c-not-met-bg);
}

.draft {
  margin: var(--space-2) 0 0;
  padding: var(--space-3);
  border-left: 3px solid var(--c-not-met);
  color: var(--c-text-2);
  white-space: pre-wrap;
  font-size: 13px;
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
  background: var(--c-surface);
}

.center {
  text-align: center;
}

.history li {
  border-bottom: 1px dashed var(--c-border);
  padding-bottom: var(--space-2);
}

.history-head {
  display: flex;
  justify-content: space-between;
  width: 100%;
  background: none;
  border: none;
  padding: var(--space-2) 0;
  cursor: pointer;
  font-family: inherit;
  font-size: 13px;
  color: var(--c-text);
  text-align: left;
  gap: var(--space-3);
}

.bubbles {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-bottom: var(--space-3);
}

.bubble {
  padding: var(--space-3);
  border-radius: var(--radius-control);
  font-size: 13px;
  line-height: 1.75;
}

.bubble p {
  margin: 0;
  white-space: pre-wrap;
}

.bubble-foot {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  margin-top: var(--space-2);
  flex-wrap: wrap;
}

.bubble.user {
  background: var(--c-primary-weak);
  align-self: flex-end;
  max-width: 80%;
}

.bubble.assistant {
  background: var(--c-bg);
  border: 1px solid var(--c-border);
}

@media (max-width: 900px) {
  .trace {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
</style>
