/**
 * 接口客户端。
 *
 * 三件事必须在这里做掉，业务组件不许重复实现：
 * 1. 统一解包响应外壳 {code,message,data,request_id,timestamp}
 * 2. 401 自动清理令牌并跳登录（避免每个页面各自判断）
 * 3. 把后端 code 翻译成可展示的错误，而不是把 JSON 直接丢给用户看
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

  constructor(code: number, message: string, requestId = '') {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.requestId = requestId
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

/** 业务态码：按契约这些返回 HTTP 200，前端应正常渲染而非弹错误 */
export const BIZ_CODES = new Set([4001, 4003, 4006])

let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
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

  if (envelope.code === 0 || BIZ_CODES.has(envelope.code)) {
    return envelope.data as T
  }

  if (envelope.code === 3001) {
    setToken(null)
    onUnauthorized?.()
  }

  throw new ApiError(envelope.code, envelope.message, envelope.request_id)
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'POST',
      body: body instanceof FormData ? body : JSON.stringify(body ?? {}),
    }),
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
