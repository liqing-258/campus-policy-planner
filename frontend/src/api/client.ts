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
  /** 降级的定位信息：缺哪个变量、找过哪些 .env。不含密钥值 */
  llm_config?: LlmConfig
  embedding_mode: 'vector' | 'bm25_only' | 'disabled'
  document_count: number
  chunk_count: number
}

export interface LlmEnvFile {
  path: string
  found: boolean
}

export interface LlmConfig {
  configured: boolean
  api_key: 'set' | 'missing'
  model: string
  base_url: 'set' | 'missing'
  missing: string[]
  env_files: LlmEnvFile[]
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

/** 数字验证码：图是服务端画好的 SVG，前端只负责显示与回传 id */
export interface Captcha {
  captcha_id: string
  /** `data:image/svg+xml;base64,...` */
  image: string
  expires_in: number
  length: number
}

export interface RegisterPayload {
  display_name: string
  student_no: string
  password?: string
  captcha_id: string
  captcha_code: string
}

export interface RegisterResult extends LoginResult {
  /** `student_no_suffix` = 没填密码，用了学号后 6 位作初始口令 */
  password_source: 'custom' | 'student_no_suffix'
  initial_password: string | null
}

// ---------- 管理员数据看板（R9）----------
export interface PortraitItem {
  session_id: string
  student: string
  student_no: string
  document_title: string
  document_id: string
  student_type: string
  grade: string
  gpa: string
  major: string
  met: number
  not_met: number
  unknown: number
  completeness: number
  created_at: string | null
}

export interface PortraitBucket {
  label: string
  count: number
}

export interface PortraitSummary {
  session_count: number
  student_count: number
  document_count: number
  recent_7d: number
  met: number
  not_met: number
  unknown: number
  judged: number
  /** 无数据时为 null，不是 0 —— 0 会被读成"一个都没符合" */
  met_rate: number | null
  unknown_rate: number | null
  avg_completeness: number | null
  by_student_type: PortraitBucket[]
  raw_student_types: PortraitBucket[]
  by_grade: PortraitBucket[]
  truncated: boolean
  generated_at: string | null
}

export interface PortraitsData {
  summary: PortraitSummary
  items: PortraitItem[]
}

export interface QaKeyword {
  word: string
  weight: number
  count: number
}

export interface QaTopic {
  key: string
  label: string
  count: number
  share: number
  examples: string[]
}

export interface QaHour {
  hour: number
  count: number
}

export interface QaSummary {
  question_count: number
  asker_count: number
  conversation_count: number
  keyword_count: number
  peak_hour: number | null
  truncated: boolean
  generated_at: string | null
}

export interface QaData {
  summary: QaSummary
  keywords: QaKeyword[]
  topics: QaTopic[]
  hours: QaHour[]
  /** 后端统一用 label/count，提问人数那一栏的"名字"也塞在 label 里 */
  weekdays: PortraitBucket[]
  top_askers: PortraitBucket[]
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
  /** R15：被哪份文件替代（人工确认的版本关联），无则为 null */
  supersedes_id: string | null
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
  /**
   * 后端按「刚答的这份文件 + 它真实存在的章节」派生的追问（R5）。
   * 有就用它——它的每一条都有语料依据；没有（旧后端）才退回前端按标签拼的模板。
   */
  next_questions?: string[]
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
  /** R5：降级模式也按章节派生追问，与有没有模型无关 */
  next_questions?: string[]
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
  /** R15：该文件存在多个版本时的核对提醒（单版本为 null） */
  version_note?: MatchVersionNote | null
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
  /**
   * 任务计数（R19 新增，仅 `GET /plans` 列表接口下发）。
   * `open` 与工作台 `/tasks/stats` 同口径：排除已完成与已取消。
   * 有了它，计划卡片不必再为每个计划单独请求一次 `/tasks?plan_id=`（原先的 N+1）。
   */
  task_counts?: { total: number; done: number; open: number }
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
  feedback: { helpful: boolean; reason_code: string | null; comment: string | null } | null
  created_at: string | null
}

export type FeedbackReason =
  | 'wrong_fact'
  | 'missing_citation'
  | 'outdated_policy'
  | 'too_vague'
  | 'irrelevant'
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

/* ============================================================
   校园事务指南（R12）
   指南没有自己的表：`document` 与 `blocks` 都是后端按请求从
   documents / document_chunks 里现取的，所以这里只描述形状。
   ============================================================ */

export interface GuideCategory {
  id: string
  label: string
  description: string
  /** 分类级覆盖说明（如"就业类政策尚未收录"），没有就是 null */
  note: string | null
  /** 编目条数 */
  item_count: number
  /** 其中有政策依据的条数 */
  available_count: number
  /** 一条政策都没有：前端显示空状态而不是编出来的卡片 */
  empty: boolean
  document_count: number
}

export interface GuideDocument {
  id: string
  title: string
  effective_status: string
  academic_year: string | null
  version_label: string | null
  department: string | null
  published_at: string | null
  parse_status: string
  chunk_count: number
  /** 已被替代时指向替代它的现行版本 */
  superseded_by: { id: string; title: string } | null
}

export interface GuideAudience {
  text: string
  /** policy = 原文里的适用条款；rules = 已复核规则的奖项标签 */
  source: 'policy' | 'rules'
  section: string | null
}

export interface GuideBlock {
  field: 'conditions' | 'materials' | 'steps'
  label: string
  section: string
  page: number | null
  article: string | null
  text: string
}

export interface GuideAvailability {
  available: boolean
  document_id: string | null
  reason: string | null
  approved_condition_rules?: number
  approved_rules?: number
}

export interface GuideActions {
  ask: { question: string; document_id: string | null }
  match: GuideAvailability
  plan: GuideAvailability
}

export interface GuideCard {
  key: string
  category: string
  category_label: string
  title: string
  summary: string
  keywords: string[]
  recommended: boolean
  /** ok = 已收录；missing_policy = 暂未收录相关政策；no_text = 文件无可解析正文 */
  availability: 'ok' | 'missing_policy' | 'no_text'
  availability_label: string
  audience: GuideAudience | null
  document: GuideDocument | null
  conditions_preview: string
  /** 卡片上的三行摘要：条件 / 材料 / 流程，都截断到一两行 */
  materials_preview: string
  steps_preview: string
  match_available: boolean
  plan_available: boolean
}

export interface GuideDetail
  extends Omit<GuideCard, 'conditions_preview' | 'materials_preview' | 'steps_preview'> {
  blocks: GuideBlock[]
  faq: string[]
  /** 与上文章节同源、因此没有重复展示的字段（如 steps） */
  duplicate_fields: string[]
  actions: GuideActions
  coverage: { sections_found: number; sections_expected: number }
  related: GuideCard[]
  source_note: string
}

export interface GuideOverview {
  categories: GuideCategory[]
  recommended: GuideCard[]
  stats: {
    category_count: number
    item_count: number
    available_count: number
    missing_count: number
    empty_category_count: number
    document_count: number
  }
  notice: string
}

// ============================================================ R13：政策推荐
/**
 * 三态与资格匹配共用 `MatchStatus`：`met` / `not_met` / `unknown`。
 * 推荐页的文案是「可能符合 / 暂不符合 / 暂无法判断」，由后端 `status_label` 给出，
 * 前端不再维护第二套映射——两处各写一遍，迟早出现同一个结论两种说法。
 */
export type RecommendStatus = MatchStatus

export interface RecommendEvidence {
  chunk_id: string
  page: number | null
  section: string | null
  article: string | null
  /** 逐字取自 document_chunks，已去掉页脚与章节标题 */
  text: string
  quote: string
}

export interface RecommendConditionResult {
  rule_code: string
  field_key: string | null
  description: string
  status: RecommendStatus
  status_label: string
  unknown_reason: UnknownReason | null
  user_value: string | null
  expected_value: string | null
  rationale: string
  evidence: { chunk_id: string; page: number | null; section: string | null; quote: string }
}

export interface RecommendAudience {
  text: string
  section: string | null
  article: string | null
  quote: string
}

/**
 * 卡片上的三个动作。`available` 由后端预判：
 * 没复核规则时匹配会吃 2008，生成计划会混入同文件其他奖项的材料，
 * 与其让用户点过去撞一次，不如在卡片上就说清为什么不能点。
 */
export interface RecommendAction {
  available: boolean
  document_id: string | null
  reason: string | null
}

export interface RecommendDetailAction extends RecommendAction {
  /** guide = 有指南详情页；document = 直接打开知识库里的那份文件 */
  kind: 'guide' | 'document' | null
  guide_key: string | null
  path: string | null
}

export interface RecommendCard {
  key: string
  title: string
  category: string
  category_label: string
  kind: 'award' | 'support'
  status: RecommendStatus
  status_label: string
  /** code 说明"为什么是这个结论"；highlights 是逐条理由（带两个数字的那种） */
  reason: { code: string; text: string; highlights: string[] }
  conditions: RecommendConditionResult[]
  conditions_preview: string
  conditions_available: boolean
  conditions_source: 'rules' | 'policy' | null
  condition_counts: {
    met: number
    not_met: number
    unknown: number
    approved: number
    pending: number
    rejected: number
  }
  missing_fields: string[]
  missing_labels: string[]
  audience: RecommendAudience | null
  evidence: RecommendEvidence | null
  document: GuideDocument | null
  target_group: string | null
  note: string | null
  ask: string
  actions: {
    detail: RecommendDetailAction
    match: RecommendAction & { target_group: string | null; approved_condition_rules: number }
    plan: RecommendAction & { approved_rules: number }
  }
  score: number
  rank: number
  sort_basis: string[]
}

export interface RecommendProfile {
  source: 'latest_match' | 'none'
  session_id: string | null
  document_id: string | null
  document_title: string | null
  submitted_at: string | null
  fields: Record<string, unknown>
  filled: number
  total: number
  completeness: number
  missing_fields: string[]
  missing_labels: string[]
}

export interface RecommendData {
  profile: RecommendProfile
  summary: {
    total: number
    met: number
    not_met: number
    unknown: number
    cataloged: number
    excluded: number
    empty: boolean
  }
  items: RecommendCard[]
  excluded: { key: string; title: string; code: string; reason: string }[]
  coverage: { label: string; status: 'covered' | 'missing' | 'excluded'; subject_key: string | null; note: string }[]
  notices: { level: 'info' | 'warn'; code: string; text: string }[]
  empty_title: string
  empty_hint: string
  disclaimer: string
  generated_at: string
}

/* ============================================================
   校园日历（R14）
   日历没有第二个任务库：`kind === 'task'` 的条目就是 tasks 表的一行，
   改期与完成都走既有的 /tasks 接口。`kind === 'event'` 才是新表
   （个人日程），`kind === 'policy'` 是"政策里写了时间、但还没生成任务"。
   ============================================================ */

export type CalendarCategory =
  | 'scholarship'
  | 'award'
  | 'exam'
  | 'material'
  | 'library'
  | 'personal'
  | 'other'

/** 日期从哪来。`confirmed` = 原文只给月日、学生补了年份 */
export type CalendarDateSource = 'policy' | 'confirmed' | 'manual'

/** 没有日期时缺的到底是什么。三种缺法三句话，不能合并 */
export type UndatedReason = 'year_missing' | 'no_date_in_policy' | 'not_set'

export interface CalendarItem {
  id: string
  /** task = 我的待办；event = 个人日程；policy = 政策里的时间节点（尚未生成任务） */
  kind: 'task' | 'event' | 'policy'
  ref_id: string
  title: string
  /** `YYYY-MM-DD`。待确认条目为 null —— 不猜年份，所以它没有日期 */
  date: string | null
  time: string | null
  due_at: string | null
  status: string
  done: boolean
  cancelled: boolean
  overdue: boolean
  priority: string | null
  date_source: CalendarDateSource | null
  date_source_label: string
  date_confirmed_by_user: boolean
  category: CalendarCategory
  category_label: string
  plan_id: string | null
  plan_title: string | null
  document_id: string | null
  document_title: string | null
  description: string | null
  required_materials: string | null
  note: string | null
  needs_human_confirmation: boolean
  completed_at: string | null
  /** 原文里的月日原样，如「9 月 10 日」；只对待确认条目有值 */
  raw_text: string | null
  undated_reason: UndatedReason | null
  undated_reason_label: string | null
  /** 只有个人日程能改能删；任务的改期走 /tasks/{id}，删除不在本期范围 */
  editable: boolean
  deletable: boolean
  index?: number
}

export interface CalendarCategoryCount {
  key: CalendarCategory
  label: string
  count: number
}

export interface CalendarData {
  range: { from: string; to: string; days: number }
  /** 服务器本地日期，用来给"今天"画圈；不要用浏览器时区另算一个 */
  today: string
  items: CalendarItem[]
  undated: CalendarItem[]
  counts: {
    total: number
    done: number
    overdue: number
    undated: number
    by_day: Record<string, number>
    by_category: Record<CalendarCategory, number>
    undated_by_category: Record<CalendarCategory, number>
  }
  categories: CalendarCategoryCount[]
  notice: string
  generated_at: string
}

export interface CalendarAgenda {
  today: string
  window_days: number
  overdue: CalendarItem[]
  soon: CalendarItem[]
  /** 只含「年份待确认」这一类：其余两类在工作台没有可操作性 */
  undated: CalendarItem[]
  counts: {
    overdue: number
    soon: number
    undated: number
    undated_year_missing: number
  }
  generated_at: string
}

export interface CalendarEventRow {
  id: string
  title: string
  date: string
  time: string | null
  note: string | null
  category: CalendarCategory
  category_label: string
  created_at: string | null
  updated_at: string | null
}

/* ============================================================
   政策版本与更新提醒（R15）
   版本关系只有一份真值：documents.supersedes_id + 主题归并（后端现算）。
   这里没有"版本表"的类型——历史版本就是 DocumentRow 的精简版。
   ============================================================ */

export type VersionMatchKind = 'same_topic' | 'similar'

export interface VersionBrief {
  document_id: string
  title: string
  file_type: string
  department: string | null
  published_at: string | null
  academic_year: string | null
  version_label: string | null
  effective_status: string
  supersedes_id: string | null
  created_at: string
  match?: VersionMatchKind
  similarity?: number
  is_current?: boolean
}

export interface VersionCandidates {
  strong: VersionBrief[]
  weak: VersionBrief[]
}

export interface DocumentVersions {
  document_id: string
  newest_document_id: string
  count: number
  versions: VersionBrief[]
  hint: string
}

export interface PolicyUpdateItem {
  document_id: string
  title: string
  department: string | null
  published_at: string | null
  academic_year: string | null
  version_label: string | null
  effective_status: string
  created_at: string
  updated_at: string
  change_kind: 'new_file' | 'meta_updated'
  /** 只有两句固定话——系统不比对条款，不允许出现任何"变化内容" */
  change_summary: string
  version_count: number
  has_newer: boolean
  newer_document_id: string | null
  related_plans: { id: string; title: string; document_id: string | null }[]
  may_affect_plans: boolean
}

export interface PolicyUpdates {
  window_days: number
  today: string
  items: PolicyUpdateItem[]
  counts: { total: number; new_file: number; meta_updated: number; plan_related: number }
  notice: string
}

export interface MatchVersionNote {
  has_multiple: boolean
  count: number
  newest_document_id: string
  versions: VersionBrief[]
  hint: string
}

/* ============================================================
   办事评价与纠错反馈（R16）
   纠错反馈是另一张表（correction_feedback），因为 qa_feedback 的
   message_id 是 NOT NULL 外键，而「这份政策写错了」不指向任何 AI 消息。
   管理员看到的是两类合并视图，公共字段同形、差异只在 context。
   ============================================================ */

export type CorrectionTarget = 'document' | 'guide' | 'match'

export interface CorrectionSubmitResult {
  feedback_id: string
  target_type: CorrectionTarget
  target_id: string
  comment: string
  reason_code: string | null
  updated: boolean
  created_at: string | null
}

export type FeedbackStatus = 'pending' | 'processing' | 'resolved'

export interface AdminFeedbackItem {
  kind: 'qa' | 'correction'
  id: string
  user: { id: string | null; display_name: string; username: string | null }
  helpful: boolean | null
  reason_code: string | null
  comment: string | null
  status: FeedbackStatus
  admin_note: string | null
  created_at: string | null
  resolved_at: string | null
  context: {
    // qa 特有：关联问题 + AI 回答 + 引用片段
    question?: string | null
    answer?: string | null
    model?: string | null
    conversation_id?: string | null
    citations?: { document_id: string; document_title: string; section_title: string | null; snippet: string }[]
    // correction 特有：目标对象 + 政策文件
    target_type?: string
    target_id?: string
    document?: { id: string; title: string; effective_status: string; department: string | null; academic_year: string | null; version_label: string | null } | null
  }
}

export interface AdminFeedbackStats {
  total: number
  qa: { total: number; helpful: number; unhelpful: number; helpful_rate: number; unhelpful_rate: number }
  correction_count: number
  policy_error_count: number
  reason_distribution: { reason_code: string; count: number }[]
  status: { pending: number; processing: number; resolved: number }
  generated_at: string
}

/* ============================================================
   常用政策收藏（R17）
   收藏是一张 user + target 关联表；这里只描述形状。四类目标：
   document=政策文件 / guide=校园事务指南 / qa=重要问答 / plan=常用办理事项。
   列表项里的 document/effective_status 是后端实时 JOIN 出来的，
   「政策更新后同步有效状态」与「问答保留原始回答来源」都靠它现算。
   ============================================================ */

export type FavoriteTarget = 'document' | 'guide' | 'qa' | 'plan'

export interface FavoriteDocument {
  id: string
  title: string
  effective_status: string
  academic_year: string | null
  version_label: string | null
  department: string | null
}

/** 问答来源引用（与 qa.py 的 citations_json 同源，收藏列表里带出） */
export interface FavoriteCitation {
  index: number
  document_id: string
  document_title: string
  page_number: number | null
  section_title: string | null
  snippet: string
}

export interface FavoriteItem {
  id: string
  target_type: FavoriteTarget
  target_type_label: string
  target_id: string
  /** 收藏时的名称快照；目标删除后仍能显示 */
  title: string
  created_at: string | null
  /** 关联政策文件（实时；目标被下架则为 null） */
  document: FavoriteDocument | null
  /** 政策有效状态（实时） */
  effective_status: string | null
  // qa 特有：原始问题 + 原始回答 + 来源 + 回答时间
  question: string | null
  answer: string | null
  citations: FavoriteCitation[] | null
  answered_at: string | null
  conversation_id: string | null
  // plan 特有
  plan_status: string | null
}

export interface FavoriteCreateResult {
  id: string
  target_type: FavoriteTarget
  target_type_label: string
  target_id: string
  title: string
  document_id: string | null
  /** true = 已存在，本次是覆盖快照 */
  updated: boolean
  created_at: string | null
}

export interface FavoriteIds {
  ids: string[]
  count: number
}

export interface FavoriteList extends Paged<FavoriteItem> {
  type: string | null
  query: string
  types: { id: FavoriteTarget; label: string }[]
}
