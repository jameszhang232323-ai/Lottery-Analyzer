/**
 * 后端 HTTP 客户端：统一前缀、同源会话与错误结构。
 *
 * 约定（设计文档 11.2）：所有请求走 `/api/v1`；错误体为
 * `code` / `message` / `details` / `request_id`。
 *
 * 安全边界（设计文档 6.4、13.1）：
 * - 服务密钥与访问令牌只由后端读取，浏览器只使用 HttpOnly Cookie；
 * - 本模块（以及整个前端）不读写 localStorage / sessionStorage / document.cookie，
 *   避免把凭证落进浏览器存储。`credentials: 'same-origin'` 即可携带会话 Cookie。
 * - 网络失败（请求未到达后端）与后端业务错误用 `ApiError.kind` 区分。
 */

import type { ApiErrorBody } from './types'

export type { ApiErrorBody, GamesResponse, GameInfo, HealthStatus } from './types'

export const API_BASE = '/api/v1'

/** 错误来源：后端业务错误、后端响应不符合契约、以及请求未能到达后端。 */
export type ApiErrorKind = 'backend' | 'unexpected' | 'network'

const NETWORK_ERROR_MESSAGE = '无法连接后端服务，请确认服务已启动后重试。'

/**
 * 统一错误对象。
 *
 * `kind` 让界面可以区分“网络不通，稍后重试”和“后端明确拒绝，需要改正输入”；
 * `requestId` 用于把界面错误与后端日志对齐。
 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>
  readonly requestId: string
  readonly kind: ApiErrorKind

  constructor(status: number, body: ApiErrorBody, kind: ApiErrorKind = 'backend') {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.details = body.details
    this.requestId = body.request_id
    this.kind = kind
  }

  get isNetworkError(): boolean {
    return this.kind === 'network'
  }

  /** 与后端错误体同名，便于日志和契约测试直接读取。 */
  get request_id(): string {
    return this.requestId
  }

  /** 错误类别的人类可读标签，界面必须同时展示文字而不只是颜色。 */
  get kindLabel(): string {
    switch (this.kind) {
      case 'network':
        return '网络不可达'
      case 'unexpected':
        return '响应异常'
      default:
        return '后端错误'
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 把任意响应体收敛成契约错误体；缺少 request_id 时回退到响应头。 */
export function toApiErrorBody(
  payload: unknown,
  requestIdHeader: string | null = null,
): ApiErrorBody | null {
  if (!isRecord(payload)) return null
  if (typeof payload.code !== 'string' || typeof payload.message !== 'string') return null
  const headerId = requestIdHeader ?? ''
  const bodyId = typeof payload.request_id === 'string' ? payload.request_id : ''
  return {
    code: payload.code,
    message: payload.message,
    details: isRecord(payload.details) ? payload.details : {},
    request_id: bodyId || headerId,
  }
}

/** 非 2xx 响应映射：符合契约的是后端错误，否则是响应异常。 */
export function mapErrorResponse(
  status: number,
  payload: unknown,
  requestIdHeader: string | null = null,
): ApiError {
  const body = toApiErrorBody(payload, requestIdHeader)
  if (body) return new ApiError(status, body, 'backend')
  return new ApiError(
    status,
    {
      code: 'UNEXPECTED_RESPONSE',
      message: `后端返回了无法识别的错误响应（HTTP ${status}）。`,
      details: {},
      request_id: requestIdHeader ?? '',
    },
    'unexpected',
  )
}

/** 请求未到达后端（DNS、连接拒绝、断网、超时、取消）时映射为网络错误。 */
export function mapNetworkError(error: unknown): ApiError {
  const reason = error instanceof Error ? error.message : String(error)
  return new ApiError(
    0,
    {
      code: 'NETWORK_ERROR',
      message: NETWORK_ERROR_MESSAGE,
      details: { reason },
      request_id: '',
    },
    'network',
  )
}

/** 把任意 catch 到的值收敛为 ApiError，便于界面统一展示。 */
export function toApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : mapNetworkError(error)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: 'same-origin',
      headers,
    })
  } catch (error) {
    throw mapNetworkError(error)
  }

  const requestId = response.headers.get('X-Request-ID')

  if (response.status === 204) {
    return undefined as T
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    throw mapErrorResponse(response.status, payload, requestId)
  }

  if (payload === null) {
    throw new ApiError(
      response.status,
      {
        code: 'UNEXPECTED_RESPONSE',
        message: '后端返回了空响应。',
        details: {},
        request_id: requestId ?? '',
      },
      'unexpected',
    )
  }

  return payload as T
}

/** GET 请求，路径相对 `/api/v1`。 */
export function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  return request<T>(path, { ...init, method: 'GET' })
}

/**
 * POST 请求，路径相对 `/api/v1`。
 *
 * `body` 为 undefined 时不写请求体；有值时按 JSON 序列化。
 * 副作用接口的幂等键与 CSRF 令牌由后续任务在同一处补充，不写入浏览器存储。
 */
export function apiPost<T>(path: string, body?: unknown, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (body !== undefined) headers.set('Content-Type', 'application/json')
  return request<T>(path, {
    ...init,
    method: 'POST',
    headers,
    body: body === undefined ? init.body : JSON.stringify(body),
  })
}
