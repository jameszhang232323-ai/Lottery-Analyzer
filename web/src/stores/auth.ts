/**
 * 会话与当前用户（Pinia，仅内存）。
 *
 * 设计边界：
 * - 会话令牌只存在于 HttpOnly Cookie，前端不读取、不复制到任何浏览器存储；
 * - 用户对象只保存在 Pinia 内存中，刷新后通过 `GET /auth/me` 重新初始化；
 * - 401 是“会话失效”的唯一信号：清空内存用户并回到登录页，不把 401 当成
 *   普通业务错误反复提示。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { Router } from 'vue-router'

import {
  apiGet,
  apiPost,
  setUnauthorizedHandler,
  toApiError,
  type ApiError,
} from '../api/client'
import type { LoginResponse, LogoutResponse, MeResponse, UserInfo } from '../api/types'
import { resetSessionState } from './sessionReset'
import { roleLabel } from './selectors'

export type AuthStatus = 'unknown' | 'loading' | 'authenticated' | 'anonymous'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  const status = ref<AuthStatus>('unknown')
  const error = ref<ApiError | null>(null)
  const initialized = ref(false)

  let inflight: Promise<void> | null = null

  const isAuthenticated = computed(() => user.value !== null && status.value === 'authenticated')
  const isAdmin = computed(() => isAuthenticated.value && user.value?.role === 'admin')
  const displayName = computed(() => user.value?.display_name || user.value?.username || '')
  const role = computed(() => (user.value ? roleLabel(user.value.role) : ''))

  function applyUser(next: UserInfo): void {
    user.value = next
    status.value = 'authenticated'
    error.value = null
    initialized.value = true
  }

  function clearMemory(): void {
    user.value = null
    status.value = 'anonymous'
    error.value = null
    initialized.value = true
  }

  /** 读取 `/auth/me`；401 视为未登录，其他错误保留真实类别供界面展示。 */
  async function fetchMe(): Promise<void> {
    status.value = 'loading'
    try {
      const payload = await apiGet<MeResponse>('/auth/me')
      applyUser(payload.user)
    } catch (caught) {
      const apiError = toApiError(caught)
      clearMemory()
      if (apiError.status !== 401) error.value = apiError
    }
  }

  /**
   * 初始化会话；并发调用共享同一个请求。
   *
   * `force` 用于登录后或需要重新确认会话时强制刷新。
   */
  async function ensureSession(force = false): Promise<void> {
    if (!force && initialized.value) return
    if (inflight) return inflight
    inflight = fetchMe().finally(() => {
      inflight = null
    })
    return inflight
  }

  /** 登录：成功后只在内存保存用户；失败保留后端错误码与网络/后端类别。 */
  async function login(username: string, password: string): Promise<boolean> {
    error.value = null
    status.value = 'loading'
    try {
      const payload = await apiPost<LoginResponse>('/auth/login', { username, password })
      applyUser(payload.user)
      return true
    } catch (caught) {
      const apiError = toApiError(caught)
      user.value = null
      status.value = 'anonymous'
      error.value = apiError
      initialized.value = true
      return false
    }
  }

  /** 退出：成功后清空内存；网络失败时保留用户并返回 false，避免误以为已撤销。 */
  async function logout(): Promise<boolean> {
    error.value = null
    try {
      await apiPost<LogoutResponse>('/auth/logout')
      clearMemory()
      resetSessionState()
      return true
    } catch (caught) {
      const apiError = toApiError(caught)
      if (apiError.status === 401) {
        clearMemory()
        resetSessionState()
        return true
      }
      error.value = apiError
      return false
    }
  }

  /** 401 时由 API 客户端回调：清空内存并回到登录页（带原路径）。 */
  function handleUnauthorized(router: Router): void {
    clearMemory()
    resetSessionState()
    const current = router.currentRoute.value
    // 首次导航尚未完成（START_LOCATION）时由路由守卫处理重定向，避免并发导航。
    if (!current.matched.length) return
    if (current.name === 'login') return
    void router.replace({ name: 'login', query: { redirect: current.fullPath } })
  }

  /** 注册全局 401 处理器；应用启动时调用一次。 */
  function installUnauthorizedHandler(router: Router): void {
    setUnauthorizedHandler(() => handleUnauthorized(router))
  }

  return {
    user,
    status,
    error,
    initialized,
    isAuthenticated,
    isAdmin,
    displayName,
    role,
    ensureSession,
    fetchMe,
    login,
    logout,
    clearMemory,
    installUnauthorizedHandler,
  }
})
