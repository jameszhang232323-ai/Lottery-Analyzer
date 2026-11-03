import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  apiGet,
  toApiError,
  withQuery,
  type ApiError,
  type ApiErrorKind,
  type GamesResponse,
  type HealthStatus,
} from '../api/client'
import type { CoverageResponse, GameInfo } from '../api/types'
import { useDrawsStore } from './draws'
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
 * 全局系统状态：后端连通性、彩种与真实数据覆盖。
 *
 * 设计 11.2 的首版接口中，健康探针、`/games`、`/draws` 与 `/coverage` 已可
 * 真实调用。完整性来自覆盖清单的真实计数；最近同步时间没有独立接口，保持
 * 未知（显示“待确认”），不从开奖观察时间推测。
 */
export const useSystemStore = defineStore('system', () => {
  const connection = ref<BackendConnectionState>('idle')
  const games = ref<GameInfo[]>([])
  const checkedAt = ref<string | null>(null)
  const lastError = ref('')
  const errorKind = ref<ApiErrorKind | null>(null)
  const lastApiError = ref<ApiError | null>(null)

  /** 开奖/覆盖接口接通后由 refreshDataset 写入真实值。 */
  const dataset = ref<DatasetState>({ ...EMPTY_DATASET })
  const coverage = ref<CoverageResponse | null>(null)
  const datasetLoadedAt = ref<string | null>(null)

  let datasetInflight: Promise<void> | null = null

  /** 后端没有 AI 配置状态接口：不宣称“未配置”，只显示状态未知。 */
  const ai = ref<AiStatusInput>({
    apiAvailable: true,
    configured: false,
    configurationKnown: false,
  })

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

  /** 真实读取健康探针与彩种清单。 */
  async function refresh(): Promise<void> {
    if (connection.value === 'checking') return
    connection.value = 'checking'
    lastError.value = ''
    errorKind.value = null
    lastApiError.value = null

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
      lastApiError.value = apiError
      checkedAt.value = new Date().toISOString()
    }
  }

  /**
   * 真实读取开奖数据与覆盖清单。
   *
   * 覆盖区间取“已取得的最新最多 300 期”，而不是从当天倒推；这样不会把尚未
   * 开奖的日历期算成缺期。接口失败时保留错误，不保留上次成功的数据。
   */
  async function loadDataset(targetGame = 'ssq', force = false): Promise<void> {
    const draws = useDrawsStore()
    lastError.value = ''
    errorKind.value = null
    lastApiError.value = null
    dataset.value = { ...dataset.value, apiAvailable: true }
    await draws.load(targetGame, force)
    if (draws.error) {
      lastError.value = draws.error.message
      errorKind.value = draws.error.kind
      lastApiError.value = draws.error
      dataset.value = { ...EMPTY_DATASET }
      coverage.value = null
      return
    }

    const latest = draws.latest
    if (!latest) {
      coverage.value = null
      dataset.value = {
        ...EMPTY_DATASET,
        apiAvailable: true,
        validDrawCount: 0,
        lastSyncedAt: null,
        integrity: 'unknown',
        verification: 'unknown',
      }
      datasetLoadedAt.value = new Date().toISOString()
      return
    }

    const range = draws.range(Math.min(300, draws.count))
    try {
      const next =
        range === null
          ? null
          : await apiGet<CoverageResponse>(
              withQuery('/coverage', {
                game: targetGame,
                start_issue: range.start_issue,
                end_issue: range.end_issue,
              }),
            )
      coverage.value = next
      const verification =
        next !== null && next.analyzable_count > 0 && next.official_verified_count >= next.analyzable_count
          ? 'official'
          : next !== null && next.acquired_count > 0
            ? 'structure_only'
            : 'unknown'
      dataset.value = {
        apiAvailable: true,
        validDrawCount: next?.analyzable_count ?? draws.count,
        latestIssue: latest.issue,
        expectedLatestIssue: null,
        lastSyncedAt: null,
        expectedDrawCount: next?.expected_count ?? null,
        missingDrawCount: next?.missing_count ?? null,
        conflictCount: next?.disputed_count ?? null,
        integrity:
          next === null
            ? 'unknown'
            : next.disputed_count > 0
              ? 'conflict'
              : next.calendar_known && next.complete
                ? 'complete'
                : next.calendar_known
                  ? 'incomplete'
                  : 'unknown',
        verification,
        rangeLabel: range ? `${range.start_issue}—${range.end_issue}` : null,
      }
      datasetLoadedAt.value = new Date().toISOString()
    } catch (error) {
      const apiError = toApiError(error)
      lastError.value = apiError.message
      errorKind.value = apiError.kind
      lastApiError.value = apiError
      coverage.value = null
      dataset.value = {
        ...EMPTY_DATASET,
        apiAvailable: true,
        validDrawCount: range?.count ?? draws.count,
        latestIssue: latest.issue,
        expectedLatestIssue: null,
        lastSyncedAt: null,
        rangeLabel: range ? `${range.start_issue}—${range.end_issue}` : null,
      }
    }
  }

  async function refreshDataset(targetGame = 'ssq', force = false): Promise<void> {
    if (datasetInflight) return datasetInflight
    datasetInflight = loadDataset(targetGame, force).finally(() => {
      datasetInflight = null
    })
    return datasetInflight
  }

  async function refreshAll(targetGame = 'ssq', force = false): Promise<void> {
    await Promise.all([refresh(), refreshDataset(targetGame, force)])
  }

  function reset(): void {
    dataset.value = { ...EMPTY_DATASET }
    coverage.value = null
    datasetLoadedAt.value = null
    lastError.value = ''
    errorKind.value = null
    lastApiError.value = null
  }

  return {
    connection,
    games,
    checkedAt,
    lastError,
    errorKind,
    lastApiError,
    dataset,
    coverage,
    datasetLoadedAt,
    ai,
    enabledGames,
    backendStatus,
    aiStatus,
    globalStatus,
    refresh,
    refreshDataset,
    refreshAll,
    reset,
  }
})
