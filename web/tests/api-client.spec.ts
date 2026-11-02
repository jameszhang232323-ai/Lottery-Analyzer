import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  ApiError,
  apiGet,
  apiPost,
  mapErrorResponse,
  mapNetworkError,
  toApiError,
} from '../src/api/client'

afterEach(() => {
  vi.unstubAllGlobals()
})

function jsonResponse(body: unknown, status: number, requestId = ''): Response {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (requestId) headers['X-Request-ID'] = requestId
  return new Response(JSON.stringify(body), { status, headers })
}

describe('mapErrorResponse', () => {
  it('把契约错误体映射为 ApiError，并保留四个字段', () => {
    const error = mapErrorResponse(
      422,
      {
        code: 'VALIDATION_ERROR',
        message: '请求参数不合法',
        details: { errors: [{ loc: ['body', 'limit'] }] },
        request_id: 'req-422',
      },
      'req-header',
    )

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(422)
    expect(error.kind).toBe('backend')
    expect(error.code).toBe('VALIDATION_ERROR')
    expect(error.message).toBe('请求参数不合法')
    expect(error.requestId).toBe('req-422')
    expect(error.request_id).toBe('req-422')
    expect(error.details).toEqual({ errors: [{ loc: ['body', 'limit'] }] })
    expect(error.isNetworkError).toBe(false)
    expect(error.kindLabel).toBe('后端错误')
  })

  it('缺少 request_id 时回退到响应头', () => {
    const error = mapErrorResponse(404, { code: 'NOT_FOUND', message: '对象不存在' }, 'req-404')
    expect(error.requestId).toBe('req-404')
  })

  it('details 非对象时归零，不把数组当对象透传', () => {
    const error = mapErrorResponse(409, {
      code: 'VERSION_CONFLICT',
      message: '版本冲突',
      details: ['unexpected'],
      request_id: 'req-409',
    })
    expect(error.details).toEqual({})
  })

  it('不符合契约的错误响应标记为响应异常', () => {
    const error = mapErrorResponse(500, { message: '内部错误' }, 'req-500')
    expect(error.kind).toBe('unexpected')
    expect(error.code).toBe('UNEXPECTED_RESPONSE')
    expect(error.status).toBe(500)
    expect(error.requestId).toBe('req-500')
    expect(error.kindLabel).toBe('响应异常')
  })
})

describe('mapNetworkError', () => {
  it('请求未到达后端时状态为 0、类别为网络错误', () => {
    const error = mapNetworkError(new TypeError('Failed to fetch'))
    expect(error.kind).toBe('network')
    expect(error.isNetworkError).toBe(true)
    expect(error.status).toBe(0)
    expect(error.code).toBe('NETWORK_ERROR')
    expect(error.message).toContain('无法连接后端服务')
    expect(error.details.reason).toBe('Failed to fetch')
    expect(error.kindLabel).toBe('网络不可达')
  })

  it('toApiError 透传已有 ApiError，其他异常按网络错误处理', () => {
    const original = new ApiError(
      403,
      { code: 'ACCESS_DENIED', message: '无权访问', details: {}, request_id: 'req-403' },
    )
    expect(toApiError(original)).toBe(original)

    const mapped = toApiError(new Error('socket hang up'))
    expect(mapped.kind).toBe('network')
    expect(mapped.details.reason).toBe('socket hang up')
  })
})

describe('apiGet / apiPost', () => {
  it('GET 使用 /api/v1 前缀、同源凭据与 JSON 接受头', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ items: [] }, 200, 'req-ok'))
    vi.stubGlobal('fetch', fetchMock)

    const payload = await apiGet<{ items: unknown[] }>('/games')
    expect(payload).toEqual({ items: [] })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${API_BASE}/games`)
    expect(init.method).toBe('GET')
    expect(init.credentials).toBe('same-origin')
    expect(new Headers(init.headers).get('Accept')).toBe('application/json')
  })

  it('非 2xx 响应抛出后端错误', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            code: 'VALIDATION_ERROR',
            message: '请求参数不合法',
            details: {},
            request_id: 'req-422',
          },
          422,
          'req-422',
        ),
      ),
    )

    await expect(apiGet('/draws')).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      code: 'VALIDATION_ERROR',
      kind: 'backend',
      requestId: 'req-422',
    })
  })

  it('网络异常映射为网络错误，而不是后端错误', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('fetch failed')
      }),
    )

    await expect(apiPost('/sync-jobs', { game: 'ssq' })).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
      kind: 'network',
      status: 0,
    })
  })

  it('POST 序列化 JSON 并设置 Content-Type', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ job_id: 'job-test' }, 202))
    vi.stubGlobal('fetch', fetchMock)

    const payload = await apiPost<{ job_id: string }>('/sync-jobs', { game: 'ssq' })
    expect(payload).toEqual({ job_id: 'job-test' })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${API_BASE}/sync-jobs`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ game: 'ssq' })
    expect(new Headers(init.headers).get('Content-Type')).toBe('application/json')
  })

  it('204 返回 undefined，不尝试解析响应体', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(null, { status: 204 })),
    )
    await expect(apiPost('/jobs/job-test/cancel')).resolves.toBeUndefined()
  })

  it('2xx 但响应体为空时按响应异常处理', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('', { status: 200 })),
    )
    await expect(apiGet('/games')).rejects.toMatchObject({
      code: 'UNEXPECTED_RESPONSE',
      kind: 'unexpected',
    })
  })
})
