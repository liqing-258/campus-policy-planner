<script setup lang="ts">
/**
 * 智能问答（M4）。
 *
 * 这一页要能同时演示三件事，缺一件就不算"可信"：
 * 1. 答得出来 —— 正文 + 每条结论后面的 [n]，点开能看到原文
 * 2. 答不出来就拒答 —— 明确说"证据不足"，并列出被闸门拦下的候选
 * 3. 未配模型时降级 —— 只给原文片段，不假装给过归纳
 *
 * 4001/4003/4006 都是 HTTP 200 的业务态，所以用 api.raw 自己按 code 分支，
 * 不能像普通接口那样 try/catch 当错误处理。
 */
import { computed, onMounted, ref } from 'vue'
import {
  ApiError,
  api,
  type AnswerData,
  type ConversationRow,
  type DegradedData,
  type Paged,
  type RefusalData,
} from '../api/client'
import { evidenceLabel, formatTime } from '../utils/format'

interface CitationRejected {
  question: string
  answer_draft: string
  rejected_indexes: number[]
  valid_indexes: number[]
  retrieval: AnswerData['retrieval']
}

interface MessageRow {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: { index: number; chunk_id: string; document_title: string; section_title: string | null; page_number: number | null }[] | null
  mode: string | null
  latency_ms: number | null
  created_at: string | null
}

const question = ref('')
const asking = ref(false)
const elapsed = ref(0)

const answer = ref<AnswerData | null>(null)
const refusal = ref<RefusalData | null>(null)
const degraded = ref<DegradedData | null>(null)
const citationRejected = ref<CitationRejected | null>(null)
const fatal = ref('')
const conversationId = ref('')

const openCitation = ref('')

const conversations = ref<ConversationRow[]>([])
const activeConversation = ref('')
const messages = ref<MessageRow[]>([])

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
      citationRejected.value = envelope.data as CitationRejected
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

const empty = computed(() => !answer.value && !refusal.value && !degraded.value
  && !citationRejected.value && !fatal.value)

const confidenceLabel: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
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

    <!-- 成功：答案 + 引用 -->
    <section v-if="answer" class="card">
      <div class="result-head">
        <h3>答案</h3>
        <span class="tag met">把握度：{{ confidenceLabel[answer.confidence] ?? answer.confidence }}</span>
      </div>
      <p class="answer-text">{{ answer.answer }}</p>
      <p class="muted tiny">
        模型 {{ answer.model }} · 检索 {{ answer.retrieval_mode }} ·
        耗时 {{ answer.latency_ms }} ms ·
        检索候选 {{ answer.retrieval.candidates.length }} 条（共命中 {{ answer.retrieval.total_hits }}）
      </p>
      <p v-if="answer.disclaimer" class="muted tiny">{{ answer.disclaimer }}</p>

      <h3 class="sub">引用片段</h3>
      <ol class="cite-list">
        <li v-for="c in answer.citations" :key="c.chunk_id">
          <button class="cite-head" @click="openCitation = openCitation === c.chunk_id ? '' : c.chunk_id">
            <span class="tag plain">[{{ c.index }}]</span>
            <strong>{{ c.document_title }}</strong>
            <span class="muted small">{{ evidenceLabel(c.page_number, c.section_title) }}</span>
            <span class="muted tiny mono">覆盖率 {{ (c.coverage * 100).toFixed(0) }}%</span>
          </button>
          <p v-if="openCitation === c.chunk_id" class="cite-body">{{ c.content }}</p>
        </li>
      </ol>
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
        如果是 4002 且消息里带 reasoning_tokens，那是模型的推理预算被耗尽（配置问题），
        不是"没有依据"。
      </p>
    </section>

    <p v-if="empty && !asking" class="muted small center">
      提一个问题试试。答案会标注来源片段，点开可以核对原文。
    </p>

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
  background: #fbfcfd;
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
  border-color: #f0dfae;
  background: #fffdf7;
}

.err-card {
  border-color: #f2d3d0;
  background: #fdf7f6;
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
  border-color: #cfe6da;
  background: #f1f8f4;
}

.tag.unknown {
  color: var(--c-unknown);
  border-color: #f0dfae;
  background: #fff8e6;
}

.tag.not_met {
  color: var(--c-not-met);
  border-color: #f2d3d0;
  background: #fdf2f1;
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

.bubble.user {
  background: var(--c-primary-weak);
  align-self: flex-end;
  max-width: 80%;
}

.bubble.assistant {
  background: var(--c-bg);
  border: 1px solid var(--c-border);
}
</style>
