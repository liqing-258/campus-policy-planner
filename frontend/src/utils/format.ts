/**
 * 展示层格式化。
 *
 * 集中在单独文件里，是因为这些映射一旦分散到各组件，就会出现
 * "同一个状态在两个页面颜色不同"、"同一份文件在两处显示不同大小"这类问题。
 * 这里只有纯函数，不引用任何组件与接口。
 */

/** 解析状态 → 中文 + 语义类名。类名直接对接到三态色（met/unknown/not_met）。 */
export const PARSE_STATUS: Record<string, { label: string; tone: 'met' | 'unknown' | 'not_met' | 'plain' }> = {
  indexed: { label: '已索引', tone: 'met' },
  chunked: { label: '已切分', tone: 'met' },
  parsed: { label: '已解析', tone: 'unknown' },
  parsing: { label: '解析中', tone: 'unknown' },
  pending: { label: '待处理', tone: 'unknown' },
  empty: { label: '无有效文本', tone: 'not_met' },
  failed: { label: '解析失败', tone: 'not_met' },
}

export function parseStatus(status: string) {
  return PARSE_STATUS[status] ?? { label: status, tone: 'plain' as const }
}

export const MATCH_TONE: Record<string, 'met' | 'not_met' | 'unknown'> = {
  met: 'met',
  not_met: 'not_met',
  unknown: 'unknown',
}

export const MATCH_LABEL: Record<string, string> = {
  met: '符合',
  not_met: '不符合',
  unknown: '无法判定',
}

export const UNKNOWN_REASON_LABEL: Record<string, string> = {
  missing_info: '缺少信息',
  ambiguous_rule: '条款主观',
  no_evidence: '缺少依据',
  conflicting_policy: '政策冲突',
}

export const RULE_TYPE_LABEL: Record<string, string> = {
  eligibility: '资格条件',
  restriction: '限制条件',
  material: '申请材料',
  deadline: '时间节点',
  process: '办理流程',
}

export const REVIEW_LABEL: Record<string, string> = {
  needs_review: '待复核',
  approved: '已通过',
  rejected: '已拒绝',
}

export const TASK_STATUS_LABEL: Record<string, string> = {
  todo: '待办',
  doing: '进行中',
  done: '已完成',
  deferred: '已延期',
  cancelled: '已取消',
}

export const PLAN_STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  confirmed: '已确认',
  archived: '已归档',
  rejected: '已否决',
}

/** 任务状态机的前端镜像。后端仍会校验，这里只用于决定按钮是否可点。 */
export const TASK_NEXT: Record<string, { to: string; label: string }[]> = {
  todo: [
    { to: 'doing', label: '开始' },
    { to: 'done', label: '完成' },
    { to: 'deferred', label: '延期' },
  ],
  doing: [
    { to: 'done', label: '完成' },
    { to: 'deferred', label: '延期' },
  ],
  deferred: [{ to: 'todo', label: '恢复' }],
  done: [{ to: 'todo', label: '撤销完成' }],
  cancelled: [],
}

export function formatBytes(size: number | null | undefined): string {
  if (!size || size < 0) return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(2)} MB`
}

/** ISO 时间 → 本地可读。后端给的是 UTC（带 Z），这里按浏览器时区显示。 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function truncate(text: string | null | undefined, max = 120): string {
  if (!text) return ''
  return text.length > max ? `${text.slice(0, max)}…` : text
}

export function evidenceLabel(page: number | null, section: string | null): string {
  const parts: string[] = []
  if (section) parts.push(section)
  if (page) parts.push(`第 ${page} 页`)
  return parts.join(' · ') || '位置未标注'
}
