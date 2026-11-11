/**
 * 接口客户端。
 *
 * 三件事必须在这里做掉，业务组件不许重复实现：
 * 1. 统一解包响应外壳 {code,message,data,request_id,timestamp}
 * 2. 401 自动清理令牌并跳登录（避免每个页面各自判断）
 * 3. 把后端 code 翻译成可展示的错误，而不是把 JSON 直接丢给用户看
 *
 * M5 新增 `api.raw()`：业务态（4001/4003/4006/2008）不是错误，
 * 但页面必须能区分"拒答"和"答出来了"，只拿 data 是分不出来的。
 */

export interface ApiEnvelope<T> {
  code: number
  message: string
  data: T | null
  request_id: string
  timestamp: string
}

export class ApiError extends Error {
  code: number
  requestId: string
  /** 业务态携带的数据（拒答候选、未复核规则数等），错误场景下为 null */
  payload: unknown

  constructor(code: number, message: string, requestId = '', payload: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.requestId = requestId
    this.payload = payload
  }
}

const TOKEN_KEY = 'zxx_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

/**
 * 业务态码：按契约这些返回 HTTP 200，前端应正常渲染而非弹错误。
 *
 * 2008 是 M5 加的：文档有规则但一条都没复核通过时，匹配接口返回
 * "先复核规则"——这是系统正常工作、只是条件不满足，不是故障。
 */
export const BIZ_CODES = new Set([4001, 4003, 4006, 2008])

let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler
}

/** 拿完整信封，不做业务态判断。给需要看 code 的页面用。 */
export async function requestEnvelope<T>(
  path: string,
  options: RequestInit = {},
): Promise<ApiEnvelope<T>> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let resp: Response
  try {
    resp = await fetch(`/api/v1${path}`, { ...options, headers })
  } catch {
    throw new ApiError(-1, '无法连接后端服务，请确认已启动 :5000')
  }

  let envelope: ApiEnvelope<T>
  try {
    envelope = (await resp.json()) as ApiEnvelope<T>
  } catch {
    throw new ApiError(-1, `后端返回了非 JSON 内容（HTTP ${resp.status}）`)
  }

  if (envelope.code === 3001) {
    setToken(null)
    onUnauthorized?.()
  }
  return envelope
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const envelope = await requestEnvelope<T>(path, options)
  if (envelope.code === 0 || BIZ_CODES.has(envelope.code)) {
    return envelope.data as T
  }
  throw new ApiError(envelope.code, envelope.message, envelope.request_id, envelope.data)
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'POST',
      body: body instanceof FormData ? body : JSON.stringify(body ?? {}),
    }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) }),
  remove: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) =>
    request<T>(path, { method: 'POST', body: form }),
  /** 返回完整信封：页面自己判断 code 是 0 还是业务态 */
  raw: <T>(path: string, body?: unknown) =>
    requestEnvelope<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) }),
}

// ---------- 领域类型（与接口清单对齐，字段一律 snake_case）----------
export interface Health {
  status: string
  version: string
  env: string
  db: string
  llm_mode: 'llm' | 'degraded'
  llm_model: string | null
  embedding_mode: 'vector' | 'bm25_only' | 'disabled'
  document_count: number
  chunk_count: number
}

export interface User {
  id: string
  username: string
  display_name: string
  role: 'student' | 'admin'
}

export interface LoginResult {
  token: string
  expires_in: number
  user: User
}

export interface Paged<T> {
  items: T[]
  page: number
  page_size: number
  total: number
  has_more: boolean
}

export type ParseStatus = 'pending' | 'parsing' | 'parsed' | 'chunked' | 'indexed' | 'empty' | 'failed'

export interface DocumentRow {
  id: string
  title: string
  file_name: string
  file_type: string
  file_size: number
  department: string | null
  published_at: string | null
  academic_year: string | null
  version_label: string | null
  effective_status: string
  scope_note: string | null
  parse_status: ParseStatus
  chunk_count: number
  created_at: string | null
  updated_at: string | null
  parse_error: string | null
  parse_hint: string | null
}

export interface ChunkRow {
  id: string
  document_id: string
  document_title: string
  chunk_index: number
  content: string
  char_start: number
  char_end: number
  page_number: number | null
  section_title: string | null
  token_count: number | null
}

export type RuleType = 'eligibility' | 'material' | 'deadline' | 'process' | 'restriction'
export type ReviewStatus = 'needs_review' | 'approved' | 'rejected'
export type MatchStatus = 'met' | 'not_met' | 'unknown'
export type UnknownReason = 'missing_info' | 'ambiguous_rule' | 'no_evidence' | 'conflicting_policy'

export interface RuleRow {
  id: string
  document_id: string
  chunk_id: string | null
  rule_code: string
  rule_type: RuleType
  description: string
  target_group: string | null
  field_key: string | null
  operator: string | null
  expected_value: string | null
  required_information: string | null
  effective_period: string | null
  page_or_section: string | null
  original_quote: string
  quote_verified: boolean
  extraction_status: string
  review_status: ReviewStatus
  confidence: string | null
  created_at: string | null
}

export interface ExtractResult {
  document_id: string
  mode: string
  model: string
  chunks: number
  extracted: number
  stored: number
  dropped: { rule_code?: string; reason: string }[]
  rules: RuleRow[]
}

export interface Candidate {
  index: number
  chunk_id: string
  document_id: string
  document_title: string
  page_number: number | null
  section_title: string | null
  score: number
  coverage: number
  content?: string
  /** 命中注入特征时的标记（M6 / T-11） */
  injection?: InjectionMark
}

export interface InjectionHit {
  code: string
  description: string
  evidence: string
}

export interface InjectionMark {
  suspected: true
  hits?: InjectionHit[]
  patterns?: string[]
  original_coverage?: number | null
  weight?: number
  note: string
}

/** 一句话概括某个片段为什么被降权；`kept=false` 表示它被覆盖率闸门挡在证据之外 */
export interface InjectionSuspect {
  chunk_id: string
  document_title: string
  patterns: string[]
  original_coverage: number
  adjusted_coverage: number
  kept: boolean
}

/** 同一政策的多个版本（M6 / T-12） */
export interface VersionConflict {
  topic: string
  documents: {
    document_id: string
    title: string
    year: number | null
    academic_year: string | null
    published_at: string | null
    effective_status: string
    version_label: string | null
  }[]
  newest_document_id: string
  day_gap: number | null
  cited_indexes: number[]
  hint: string
}

// ---------- 工作流（M6）----------
export type NodeStatus = 'succeeded' | 'degraded' | 'failed' | 'skipped'

export interface WorkflowNode {
  node: 'intent' | 'retrieval' | 'analysis' | 'matching' | 'planning' | 'validation'
  label: string
  status: NodeStatus
  ms: number
  summary: string
  model_calls: number
  detail?: Record<string, unknown>
  error?: string
}

export interface Workflow {
  run_id: string
  run_type: 'qa' | 'match' | 'plan'
  status: 'running' | 'succeeded' | 'degraded' | 'failed'
  outcome: 'answered' | 'refused' | 'citation_rejected' | 'degraded' | 'failed' | null
  total_ms: number
  model_calls: number
  created_at: string | null
  nodes: WorkflowNode[]
  /** 真正执行过的节点数（`skipped` 不计） */
  executed_count: number
  node_count: number
  error?: string
}

export interface RetrievalInfo {
  mode: string
  min_coverage: number
  candidates: Candidate[]
  total_hits: number
  index_size: number
  refused: boolean
  refuse_reason: string | null
  conflicts?: VersionConflict[]
  injection_suspected?: InjectionSuspect[]
}

export interface Citation extends Candidate {}

export interface NextAction {
  action: 'match' | 'plan'
  label: string
  reason: string
}

export interface AnswerData {
  answer: string
  citations: Citation[]
  confidence: 'high' | 'medium' | 'low'
  mode: string
  retrieval_mode: string
  model: string
  latency_ms: number
  usage: {
    prompt_tokens: number | null
    completion_tokens: number | null
    reasoning_tokens: number | null
  }
  conversation_id: string
  message_id: string
  retrieval: RetrievalInfo
  disclaimer?: string
  // ---------- M6 ----------
  matter: { type: string; label: string }
  next_actions: NextAction[]
  /** 本轮带了几轮历史进 Prompt */
  history_pairs: number
  conflicts: VersionConflict[]
  injection_suspected: InjectionSuspect[]
  workflow: Workflow
  total_ms: number
  citation_warnings?: {
    rejected_indexes: number[]
    valid_indexes: number[]
    note: string
  }
}

export interface RefusalData {
  question: string
  refused: true
  reason: string
  retrieval: RetrievalInfo
  rejected_candidates: Candidate[]
  suggestions: string[]
  conversation_id?: string
  conflicts?: VersionConflict[]
  injection_suspected?: InjectionSuspect[]
  workflow?: Workflow
}

export interface DegradedData {
  question: string
  answer: null
  note: string
  retrieval: RetrievalInfo
  candidates: Candidate[]
  conversation_id?: string
  conflicts?: VersionConflict[]
  injection_suspected?: InjectionSuspect[]
  workflow?: Workflow
}

/** 引用被拒（4003）：模型给了答案但没有一条引用能落到原文 */
export interface CitationRejectedData {
  question: string
  answer_draft: string
  rejected_indexes: number[]
  valid_indexes: number[]
  retrieval: RetrievalInfo
  conflicts?: VersionConflict[]
  injection_suspected?: InjectionSuspect[]
  workflow?: Workflow
}

export interface Evidence {
  chunk_id: string | null
  page: number | null
  section: string | null
  quote: string
}

export interface MatchItem {
  rule_id: string
  rule_code: string
  rule_type: RuleType
  description: string
  status: MatchStatus
  unknown_reason: UnknownReason | null
  user_value: string | null
  expected_value: string | null
  rationale: string
  evidence: Evidence
}

export interface MatchData {
  session_id: string
  document_id: string
  profile: Record<string, unknown>
  items: MatchItem[]
  summary: { met: number; not_met: number; unknown: number; total: number }
  disclaimer: string
  created_at?: string | null
  /** 本次按哪个奖项过滤（M6）；为空表示没过滤 */
  target_group?: string | null
  /** 被奖项过滤掉的条件规则条数 */
  filtered_out?: number
}

export interface DraftTask {
  temp_id: string
  title: string
  description: string | null
  priority: string
  due_at: string | null
  due_source: string | null
  needs_human_confirmation: boolean
  source_rule_code: string | null
  required_materials: string | null
  dependency_ids: string[]
  note: string | null
}

export interface DraftData {
  plan_id: string
  document_id: string
  title: string
  status: string
  match_session_id: string | null
  tasks: DraftTask[]
  notes: string[]
  /** 任务是否已入库。注意 plan 行在草稿阶段就已写库，确认时复用同一 id */
  tasks_persisted: boolean
  plan_status: 'draft' | 'confirmed' | 'archived' | 'rejected'
  note: string
}

export interface PlanRow {
  id: string
  title: string
  status: 'draft' | 'confirmed' | 'archived' | 'rejected'
  document_id: string
  match_session_id: string | null
  created_at: string | null
  confirmed_at: string | null
}

export type TaskStatus = 'todo' | 'doing' | 'done' | 'deferred' | 'cancelled'

export interface TaskRow {
  id: string
  plan_id: string
  title: string
  description: string | null
  required_materials: string | null
  priority: string
  status: TaskStatus
  due_at: string | null
  due_source: string | null
  needs_human_confirmation: boolean
  dependency_ids: string[]
  /** 为什么缺日期、需要人工确认什么（来自规则，确认时一并落库） */
  note: string | null
  /** 溯源：这条任务由哪条规则生成 */
  source_rule_id: string | null
  sort_order: number
  completed_at: string | null
  created_at: string | null
}

export interface ConversationRow {
  id: string
  title: string
  created_at: string | null
  updated_at: string | null
  message_count?: number
}

export interface MessageRow {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[] | null
  model: string | null
  latency_ms: number | null
  mode: string | null
  /** 这条答案对应的运行记录；为空表示历史数据或降级前的记录 */
  workflow_run_id: string | null
  /** 我对这条答案的反馈（没评过就是 null） */
  feedback: { helpful: boolean; reason_code: string | null } | null
  created_at: string | null
}

export type FeedbackReason =
  | 'wrong_fact'
  | 'missing_citation'
  | 'outdated_policy'
  | 'too_vague'
  | 'other'

export interface FeedbackResult {
  feedback_id: string
  message_id: string
  helpful: boolean
  reason_code: string | null
  comment: string | null
  /** true = 覆盖了之前的反馈（同一个人对同一条答案只留一条） */
  updated: boolean
  created_at: string | null
}

/** 任务统计（M6）：口径由后端定义，前端不自己数 */
export interface TaskStats {
  total: number
  open: number
  by_status: Record<TaskStatus, number>
  by_priority: Record<string, number>
  overdue: number
  due_soon: number
  completion_rate: number
  plans: Record<string, number>
  generated_at: string
}
