import { defineStore } from 'pinia'
import { api, type Citation, type MessageRow, type Paged } from '../api/client'

/**
 * 问答会话的跨页留存。
 *
 * **它解决的是"切页丢对话"**：Vue 的 `<router-view>` 切走时 AskView 会被卸载，
 * 组件里的 `answer` / `refusal` / `degraded` 三个 ref 一起没了。用户点一下「政策知识库」
 * 再点回来，刚才那段解答就只剩一片欢迎页——而数据其实一直躺在库里。
 *
 * 三条设计取舍：
 *
 * 1. **`sessionStorage` 而不是 `localStorage`**：这是"这个标签页里刚才在聊什么"，
 *    不是"这个人上次聊到哪"。关掉标签页就该忘掉；同一个浏览器开两个标签页各聊各的，
 *    也不该互相串台。localStorage 会让第二个标签页继承第一个的半截会话。
 * 2. **写盘防抖 300ms**：一次回答会连续追加两条消息（提问 + 解答），
 *    流式/连续追问时更密。每追加一次就 JSON.stringify 整个会话，
 *    消息多了以后主线程会被咬住，表现出来是"输入框打字一顿一顿"。
 * 3. **只存 role / content / timestamp + 精简引用**：本地缓存的职责是"先让用户看见
 *    刚才聊到哪"，不是完整数据源。原文片段动辄上千字，存进去 sessionStorage 很快到
 *    5MB 上限，超了会抛 QuotaExceededError——那才是真的丢数据。
 *    需要完整内容时按 `conversation_id` 走接口取（见 AskView 的 hydrate）。
 */

const KEY = 'campus_qa_session'
const DEBOUNCE_MS = 300
/** 本地最多留多少条消息。再多就该靠接口取，而不是继续往 sessionStorage 里堆 */
const MAX_MESSAGES = 40
/** 单条消息正文最多缓存多少字。降级模式会把整段原文拼成正文，不截断能到几十 KB */
const MAX_CONTENT_CHARS = 600

/**
 * 把正文裁到上限。
 *
 * 为什么要裁：`sessionStorage` 的配额是 5MB 量级，看起来很大，
 * 但降级模式的一条回答是"6 个片段全文拼接"，单个片段 600 字很常见——
 * 一轮就 3～4 KB。不裁的话，攒到二三十轮就会抛 `QuotaExceededError`，
 * 而那时整份缓存**一次都写不进去**（不是只丢最新一条），切页会退化成全丢。
 * 裁掉的部分不影响使用：要看全文，按 `conversation_id` 走接口取。
 */
function clip(text: string): string {
  if (text.length <= MAX_CONTENT_CHARS) return text
  return `${text.slice(0, MAX_CONTENT_CHARS)}…`
}

export interface QACitation {
  index: number
  document_id: string
  document_title: string
  section_title: string | null
  page_number: number | null
  /** 一句话出处摘要，用于"切回来还能看到引用"；不存 content 全文 */
  snippet: string
}

export interface QAMessage {
  role: 'user' | 'assistant'
  content: string
  /** ISO 时间串，前端自己生成——它只是"这条是什么时候出现的" */
  timestamp: string
  /** 这一轮的业务态。答案之外的三种（拒答/降级/引用被拒）也要能复现出来 */
  kind?: QAMessageKind
  citations?: QACitation[]
  detail?: string
}

export type QAMessageKind = 'answer' | 'refusal' | 'degraded' | 'citation_rejected' | 'fatal'

/** 合法的业务态取值。缓存读回来要按它校验，见 `restore()` */
const KINDS = new Set<string>(['answer', 'refusal', 'degraded', 'citation_rejected', 'fatal'])

interface QAState {
  /**
   * 当前会话 id。无会话时是**空串**而不是 `null`。
   *
   * 设计说明书写的是 `string | null`，这里刻意用空串：调用点有七八处
   * （拼请求体、判续聊、清空、模板 `v-if`），每个都要写 `?? ''` 只多一种出错方式，
   * 而空串在模板插值、`v-if`、`Boolean()` 里都已经是安全的假值。
   */
  currentConversationId: string
  messages: QAMessage[]
  /** 轨迹详情只活在这一页里，不进本地缓存（体积大、且刷新即过期） */
  workflowTrace: unknown | null
  /**
   * 防抖写盘的定时器句柄。放进 state 是因为 Pinia 的实例类型不接受额外属性，
   * 挂 `this.persistTimer` 编译就不过（TS2339）。
   */
  persistTimer: number
}

/**
 * 从一条后端消息里抽出要缓存的引用。
 *
 * 字段做白名单而不是整条存：后端将来给 citation 加字段（比如命中词、注入标记），
 * 缓存里不该跟着膨胀——那些字段本地不需要，接口取的时候自然会有。
 */
function slimCitations(raw: unknown): QACitation[] {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    .map((item) => ({
      index: Number(item.index ?? 0),
      document_id: String(item.document_id ?? ''),
      document_title: String(item.document_title ?? ''),
      section_title: (item.section_title as string | null) ?? null,
      page_number: (item.page_number as number | null) ?? null,
      snippet: String(item.snippet ?? '').slice(0, 160),
    }))
    .filter((item) => item.document_title)
}

/** 后端消息 → 本地消息。两个链路共用同一形态，避免"刷新前后长得不一样" */
export function fromServerMessage(row: MessageRow): QAMessage {
  // 降级模式落库的助手消息正文是空串（没做归纳，只回了原文片段）。
  // 原样映射会渲染成一个没有内容的"解答"块，看起来像 bug；这里给它一句说明。
  const degradedTurn = row.role === 'assistant' && row.mode === 'degraded'
  return {
    role: row.role,
    content: row.content
      || (degradedTurn ? '（降级模式：本轮未做归纳，只返回了检索到的原文片段）' : ''),
    timestamp: row.created_at ?? new Date().toISOString(),
    kind: degradedTurn ? 'degraded' : 'answer',
    citations: slimCitations(row.citations as Citation[] | null),
  }
}

export const useQAStore = defineStore('qa', {
  state: (): QAState => ({
    currentConversationId: '',
    messages: [],
    workflowTrace: null,
    persistTimer: 0,
  }),

  getters: {
    /** 最后一条助手消息——界面上"这一轮的解答"就是它 */
    lastAssistant: (state): QAMessage | null => {
      for (let i = state.messages.length - 1; i >= 0; i -= 1) {
        if (state.messages[i].role === 'assistant') return state.messages[i]
      }
      return null
    },
    isEmpty: (state) => state.messages.length === 0,
  },

  actions: {
    /**
     * 追加一条并**防抖**写盘。
     *
     * 为什么先改内存再写盘：界面读的是 `messages`，写盘只是为了"切回来还在"。
     * 写盘失败（配额满、隐私模式禁用 storage）不能影响界面——
     * 所以 `persist` 里把异常吃掉，最多是切页丢记录，不会白屏。
     */
    appendMessage(message: QAMessage) {
      this.messages.push(message)
      if (this.messages.length > MAX_MESSAGES) {
        this.messages.splice(0, this.messages.length - MAX_MESSAGES)
        // 裁完可能以一条"孤零零的回答"开头（用户那句提问被切掉了），
        // 铺出来就是"没有问题的答案"。挪到下一个提问处开始，语义才完整。
        while (this.messages.length && this.messages[0].role === 'assistant') {
          this.messages.shift()
        }
      }
      this.schedulePersist()
    },

    /**
     * 切到另一条会话（比如点右侧「最近提问」里的旧会话）。
     *
     * **必须同时清空 messages**：否则 store 里留着上一条会话的问答，
     * 用户在这条新会话里再问一句，缓存就变成"两段不相干的对话拼在一起"，
     * 切页回来看到的是一段并不存在的历史。
     */
    switchConversation(id: string) {
      if (id === this.currentConversationId) return
      this.messages = []
      this.workflowTrace = null
      this.currentConversationId = id
      this.schedulePersist()
    },

    setConversationId(id: string) {
      this.currentConversationId = id
      this.schedulePersist()
    },

    setWorkflowTrace(trace: unknown) {
      this.workflowTrace = trace
    },

    /** 用接口取回的完整消息替换本地缓存（顺序以服务端为准） */
    replaceMessages(rows: MessageRow[]) {
      this.messages = rows.map(fromServerMessage)
      this.writeNow()
    },

    schedulePersist() {
      if (this.persistTimer) window.clearTimeout(this.persistTimer)
      this.persistTimer = window.setTimeout(() => this.writeNow(), DEBOUNCE_MS)
    },

    writeNow() {
      if (this.persistTimer) {
        window.clearTimeout(this.persistTimer)
        this.persistTimer = 0
      }
      try {
        sessionStorage.setItem(KEY, JSON.stringify({
          conversationId: this.currentConversationId,
          // 只存必要字段：正文裁到上限，引用只留摘要（见文件头第 3 条取舍）。
          // 在**写盘**这一处裁而不是在 append 时裁：界面拿到的仍是全文，
          // 被裁剪的只有"下次刷新还能不能看到那句话的结尾"。
          messages: this.messages.map((m) => ({
            role: m.role,
            content: clip(m.content),
            timestamp: m.timestamp,
            kind: m.kind,
            citations: m.citations?.map((c) => ({
              index: c.index,
              document_id: c.document_id,
              document_title: c.document_title,
              section_title: c.section_title,
              page_number: c.page_number,
              snippet: c.snippet,
            })),
          })),
        }))
      } catch {
        /* 配额满或被禁用：宁可这次不缓存，也不能让提问流程崩掉 */
      }
    },

    /**
     * 仅新建对话时清空。**卸载页面严禁调用它**——那正是"切页丢记录"的成因。
     */
    clear() {
      this.messages = []
      this.currentConversationId = ''
      this.workflowTrace = null
      if (this.persistTimer) {
        window.clearTimeout(this.persistTimer)
        this.persistTimer = 0
      }
      try {
        sessionStorage.removeItem(KEY)
      } catch {
        /* 同上，忽略 */
      }
    },

    /**
     * 从 sessionStorage 恢复。
     *
     * **必须有 try-catch 且失败时清掉该项**：JSON 坏了（老版本格式、手动改过、
     * 或者上次写到一半进程被杀）如果不清理，每次进页面都会解析失败并抛错，
     * 页面直接白——而用户什么也没做错。宁可丢掉这份缓存，也不要让页面起不来。
     */
    restore(): { conversationId: string; messages: QAMessage[] } | null {
      let raw: string | null = null
      try {
        raw = sessionStorage.getItem(KEY)
      } catch {
        return null
      }
      if (!raw) return null

      try {
        const parsed = JSON.parse(raw) as Partial<{
          conversationId: string
          messages: QAMessage[]
        }>
        const messages = Array.isArray(parsed.messages)
          ? parsed.messages
            .filter((m): m is QAMessage =>
              Boolean(m) && (m.role === 'user' || m.role === 'assistant')
              && typeof m.content === 'string')
            .map((m) => ({
              // 字段逐个白名单化：缓存可以被手改、也可能是上个版本写的，
              // 直接把里面的 kind / citations 交给模板渲染等于让外部数据决定 UI 分支
              role: m.role,
              content: m.content,
              timestamp: typeof m.timestamp === 'string'
                ? m.timestamp : new Date().toISOString(),
              kind: KINDS.has(String(m.kind)) ? m.kind : 'answer',
              citations: slimCitations(m.citations),
            }))
          : []
        const conversationId = typeof parsed.conversationId === 'string'
          ? parsed.conversationId
          : ''
        if (!messages.length && !conversationId) {
          // 空壳没有保留价值；留着会让"有没有历史"的判断多一种中间态
          sessionStorage.removeItem(KEY)
          return null
        }

        this.currentConversationId = conversationId
        this.messages = messages
        return { conversationId, messages }
      } catch {
        try {
          sessionStorage.removeItem(KEY)
        } catch {
          /* 连删都删不掉（storage 被禁用）——那就当没有缓存 */
        }
        return null
      }
    },

    /**
     * 缓存为空但记得会话 id 时，从服务端把历史拉回来。
     *
     * 这条是"切回来还在"的**兜底**：用户清过浏览器缓存、或者换了个标签页，
     * sessionStorage 里什么都没有，但 `conversation_id` 还记着（例如从知识库
     * 带 query 跳过来）。这时不查接口就只能显示空态，看起来像"记录丢了"。
     */
    async hydrate(conversationId: string): Promise<boolean> {
      if (!conversationId) return false
      try {
        const data = await api.get<Paged<MessageRow>>(
          `/conversations/${conversationId}/messages?page=1&page_size=40`)
        if (!data.items.length) return false
        this.currentConversationId = conversationId
        this.messages = data.items.map(fromServerMessage)
        this.writeNow()
        return true
      } catch {
        return false
      }
    },
  },
})
