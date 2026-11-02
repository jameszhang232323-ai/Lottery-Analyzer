import { defineStore } from 'pinia'
import { ref } from 'vue'

import { apiGet, type GameInfo, type GamesResponse, type HealthStatus } from '../api/client'

export type SystemStatus = 'idle' | 'loading' | 'ok' | 'error'

/**
 * 全局数据状态：后端连通性、彩种与最近一次错误。
 *
 * 这里只保存“能不能用”，不缓存开奖数据；正式数据一律以后端快照为准。
 */
export const useSystemStore = defineStore('system', () => {
  const status = ref<SystemStatus>('idle')
  const games = ref<GameInfo[]>([])
  const lastError = ref<string>('')

  async function refresh(): Promise<void> {
    status.value = 'loading'
    lastError.value = ''
    try {
      await apiGet<HealthStatus>('/health/ready')
      const payload = await apiGet<GamesResponse>('/games')
      games.value = payload.items
      status.value = 'ok'
    } catch (error) {
      status.value = 'error'
      lastError.value = error instanceof Error ? error.message : String(error)
    }
  }

  return { status, games, lastError, refresh }
})
