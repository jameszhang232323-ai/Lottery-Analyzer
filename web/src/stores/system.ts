import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  apiGet,
  toApiError,
  type ApiErrorKind,
  type GamesResponse,
  type HealthStatus,
} from '../api/client'
import type { GameInfo } from '../api/types'
import {
  buildGlobalStatus,
  describeAi,
  describeBackend,
  EMPTY_DATASET,
  type AiStatusInput,
  type BackendConnectionState,
  type DatasetState,
} from './selectors'

/**
 * 全局系统状态：后端连通性与真实可读的基础数据。
 *
 * 设计 11.2 的首版接口中，健康探针与 `/games` 已可真实调用；开奖、任务、AI
 * 等接口尚未接入，因此 `dataset.apiAvailable` 保持 false，所有数据字段如实
 * 显示“未接入”，不用任何本地编造值填充。这里也不缓存开奖数据，正式数据
 * 一律以后端快照为准。
 */
export const useSystemStore = defineStore('system', () => {
  const connection = ref<BackendConnectionState>('idle')
  const games = ref<GameInfo[]>([])
  const checkedAt = ref<string | null>(null)
  const lastError = ref('')
  const errorKind = ref<ApiErrorKind | null>(null)

  /** 开奖数据接口未接入：字段保持 null，由 selector 统一渲染为“—”与“未接入”。 */
  const dataset = ref<DatasetState>({ ...EMPTY_DATASET })

  /** AI 接口未接入；即使以后接入，也只在后端标记可用后才显示为可用。 */
  const ai = ref<AiStatusInput>({ apiAvailable: false, configured: false })

  const enabledGames = computed(() => games.value.filter((game) => game.enabled))

  const backendStatus = computed(() =>
    describeBackend({
      state: connection.value,
      gameCount: games.value.length,
      lastError: lastError.value,
    }),
  )

  const aiStatus = computed(() => describeAi(ai.value))

  const globalStatus = computed(() =>
    buildGlobalStatus({
      backend: {
        state: connection.value,
        gameCount: games.value.length,
        lastError: lastError.value,
      },
      games: games.value,
      dataset: dataset.value,
    }),
  )

  /**
   * 真实读取健康探针与彩种清单。
   *
   * 三个请求都走同一个 `/api/v1` 前缀与同源 Cookie；失败时只记录真实错误，
   * 不保留“上次成功”的假象——连接状态必须反映本次检测结果。
   */
  async function refresh(): Promise<void> {
    if (connection.value === 'checking') return
    connection.value = 'checking'
    lastError.value = ''
    errorKind.value = null

    try {
      const [live, ready, gamesPayload] = await Promise.all([
        apiGet<HealthStatus>('/health/live'),
        apiGet<HealthStatus>('/health/ready'),
        apiGet<GamesResponse>('/games'),
      ])

      games.value = gamesPayload.items
      checkedAt.value = new Date().toISOString()

      if (live.status === 'ok' && ready.status === 'ready') {
        connection.value = 'connected'
      } else {
        connection.value = 'disconnected'
        errorKind.value = 'backend'
        lastError.value = `健康探针返回 live=${live.status}、ready=${ready.status}`
      }
    } catch (error) {
      const apiError = toApiError(error)
      connection.value = 'disconnected'
      errorKind.value = apiError.kind
      lastError.value = apiError.message
      checkedAt.value = new Date().toISOString()
    }
  }

  return {
    connection,
    games,
    checkedAt,
    lastError,
    errorKind,
    dataset,
    ai,
    enabledGames,
    backendStatus,
    aiStatus,
    globalStatus,
    refresh,
  }
})
