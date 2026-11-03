/**
 * 开奖数据缓存（Pinia，仅内存）。
 *
 * 最新一期优先用 `GET /draws/latest`，其余区间计算仍基于翻页缓存；
 * 刷新页面会重新读取，不写浏览器存储。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { fetchAllDraws, fetchLatestDraw, latestDraw, recentDraws, recentRange } from '../api/draws'
import { toApiError, type ApiError } from '../api/client'
import type { Draw } from '../api/types'

export const useDrawsStore = defineStore('draws', () => {
  const items = ref<Draw[]>([])
  const remoteLatest = ref<Draw | null>(null)
  const game = ref('ssq')
  const loading = ref(false)
  const error = ref<ApiError | null>(null)
  const loadedAt = ref<string | null>(null)

  let inflight: Promise<void> | null = null

  const latest = computed(() => remoteLatest.value ?? latestDraw(items.value))
  const count = computed(() => items.value.length)
  const latestIssue = computed(() => latest.value?.issue ?? null)

  async function load(targetGame = 'ssq', force = false): Promise<void> {
    if (!force && loadedAt.value && game.value === targetGame) return
    if (inflight) return inflight

    loading.value = true
    error.value = null
    inflight = (async () => {
      try {
        const next = await fetchAllDraws(targetGame)
        items.value = next
        game.value = targetGame
        loadedAt.value = new Date().toISOString()
      } catch (caught) {
        error.value = toApiError(caught)
      } finally {
        loading.value = false
        inflight = null
      }
    })()
    return inflight
  }

  async function loadLatest(targetGame = 'ssq'): Promise<void> {
    try {
      remoteLatest.value = await fetchLatestDraw(targetGame)
    } catch {
      // 最新接口失败不影响分页缓存；页面仍可显示已加载数据。
      remoteLatest.value = null
    }
  }

  function recent(size: number): Draw[] {
    return recentDraws(items.value, size)
  }

  function range(size: number): { start_issue: string; end_issue: string; count: number } | null {
    return recentRange(items.value, size)
  }

  function clear(): void {
    items.value = []
    remoteLatest.value = null
    loadedAt.value = null
    error.value = null
  }

  return {
    items,
    game,
    loading,
    error,
    loadedAt,
    latest,
    count,
    latestIssue,
    load,
    loadLatest,
    recent,
    range,
    clear,
  }
})
