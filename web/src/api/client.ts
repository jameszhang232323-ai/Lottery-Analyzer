/**
 * 后端 HTTP 客户端：统一前缀、同源会话、CSRF 与错误结构。
 *
 * 约定（设计文档 11.2、13.1）：
 * - 所有请求走 `/api/v1`；错误体为 `code` / `message` / `details` / `request_id`；
 * - 浏览器只使用 HttpOnly 会话 Cookie；CSRF 值从非 HttpOnly 的
 *   `dajidali_csrf` Cookie 读取，并对所有非安全方法附带 `X-CSRF-Token`；
 * - 本模块不读写 localStorage / sessionStorage，也不写 Cookie；
 * - 网络失败（请求未到达后端）与后端业务错误用 `ApiError.kind` 区分；
 * - 收到 401 时通知已注册的处理器清空会话并回到登录页。
 */

import type { ApiErrorBody } from './types'

export type {
  ApiErrorBody,
  CreateJobResponse,
  GamesResponse,
  GameInfo,
  HealthStatus,
} from './types'

export const API_BASE = '/api/v1'
export const SESSION_COOKIE_NAME = 'dajidali_session'
export const CSRF_COOKIE_NAME = 'dajidali_csrf'
export const CSRF_HEADER_NAME = 'X-CSRF-Token'

/** 错误来源：后端业务错误、后端响应不符合契约、请求未到达后端，以及本地输入问题。 */
export type ApiErrorKind = 'backend' | 'unexpected' | 'network' | 'client'

const NETWORK_ERROR_MESSAGE = '无法连接后端服务，请确认服务已启动后重试。'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE'])

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

  get isAuthRequired(): boolean {
    return this.status === 401
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
      case 'client':
        return '输入问题'
      default:
        return '后端错误'
    }
  }
}

/** 401 处理器：由会话层注册，负责清空内存会话并跳转登录页。 */
export type UnauthorizedHandler = (error: ApiError) => void

let unauthorizedHandler: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

export function isSafeMethod(method: string): boolean {
  return SAFE_METHODS.has(method.toUpperCase())
}

/** 从 Cookie 头文本读取指定名称的值；值保持原样（base64url 不含分隔符）。 */
export function readCookieValue(cookieHeader: string, name: string): string {
  if (!cookieHeader) return ''
  for (const part of cookieHeader.split(';')) {
    const index = part.indexOf('=')
    if (index < 0) continue
    const key = part.slice(0, index).trim()
    if (key !== name) continue
    return part.slice(index + 1).trim()
  }
  return ''
}

/** 从 Cookie 头文本提取 CSRF 令牌；不存在时返回 null，而不是空串。 */
export function csrfTokenFrom(cookieHeader: string | null | undefined): string | null {
  const token = readCookieValue(cookieHeader ?? '', CSRF_COOKIE_NAME)
  return token || null
}

function currentCookieHeader(): string {
  return typeof document === 'undefined' ? '' : document.cookie
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

/** 本地输入校验错误：不冒充后端错误，也不归入网络不可达。 */
export function clientError(message: string, code = 'INVALID_INPUT'): ApiError {
  return new ApiError(
    0,
    { code, message, details: {}, request_id: '' },
    'client',
  )
}

/** 从 Content-Disposition 取文件名；优先 RFC 5987 的 filename*。 */
export function filenameFromDisposition(header: string | null): string {
  if (!header) return ''
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header)
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1].trim().replace(/^"|"$/g, ''))
    } catch {
      return encoded[1].trim().replace(/^"|"$/g, '')
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header)
  return plain?.[1]?.trim() ?? ''
}

async function readResponsePayload(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return null
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')

  if (!isSafeMethod(method)) {
    const token = csrfTokenFrom(currentCookieHeader())
    if (token) headers.set(CSRF_HEADER_NAME, token)
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      method,
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

  const payload = await readResponsePayload(response)

  if (!response.ok) {
    const apiError = mapErrorResponse(response.status, payload, requestId)
    if (apiError.status === 401) unauthorizedHandler?.(apiError)
    throw apiError
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
 * `body` 为 undefined 时不写请求体；有值时按 JSON 序列化。非安全方法自动附带
 * 当前会话绑定的 CSRF 令牌（由 Cookie 读取，不写入浏览器存储）。
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

/** 附件下载：用同源 fetch 携带会话 Cookie，不把令牌放进 URL。 */
export async function apiDownload(
  path: string,
): Promise<{ blob: Blob; filename: string; contentType: string }> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: '*/*' },
    })
  } catch (error) {
    throw mapNetworkError(error)
  }

  if (!response.ok) {
    const payload = await readResponsePayload(response)
    const apiError = mapErrorResponse(response.status, payload, response.headers.get('X-Request-ID'))
    if (apiError.status === 401) unauthorizedHandler?.(apiError)
    throw apiError
  }

  return {
    blob: await response.blob(),
    filename: filenameFromDisposition(response.headers.get('Content-Disposition')),
    contentType: response.headers.get('Content-Type') ?? 'application/octet-stream',
  }
}

/** 统一构造带查询参数的 GET 路径；空值不写入查询串。 */
export function withQuery(
  path: string,
  query: Record<string, string | number | boolean | null | undefined>,
): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === '') continue
    params.set(key, String(value))
  }
  const text = params.toString()
  return text ? `${path}?${text}` : path
}
