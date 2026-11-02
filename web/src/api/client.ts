/**
 * 后端 HTTP 客户端：统一前缀、同源会话与错误结构。
 *
 * 约定（设计文档 11.2）：所有请求走 ``/api/v1``；错误体为
 * ``code`` / ``message`` / ``details`` / ``request_id``。
 * 浏览器不保存服务密钥，凭据由后端 HttpOnly Cookie 管理。
 */

const API_BASE = '/api/v1'

export interface ApiErrorBody {
  code: string
  message: string
  details: Record<string, unknown>
  request_id: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string
  readonly details: Record<string, unknown>

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.requestId = body.request_id
    this.details = body.details
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Record<string, unknown>
  return typeof candidate.code === 'string' && typeof candidate.message === 'string'
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(init.headers ?? {}),
    },
  })

  if (response.status === 204) {
    return undefined as T
  }

  const payload: unknown = await response.json().catch(() => null)

  if (!response.ok) {
    if (isApiErrorBody(payload)) {
      throw new ApiError(response.status, payload)
    }
    throw new ApiError(response.status, {
      code: 'UNEXPECTED_RESPONSE',
      message: `后端返回了无法识别的响应（HTTP ${response.status}）`,
      details: {},
      request_id: response.headers.get('X-Request-ID') ?? '',
    })
  }

  return payload as T
}

export function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  return request<T>(path, { ...init, method: 'GET' })
}

export interface HealthStatus {
  status: string
  checks: Record<string, string>
}

export interface GameInfo {
  code: string
  name: string
  enabled: boolean
}

export interface GamesResponse {
  items: GameInfo[]
}
