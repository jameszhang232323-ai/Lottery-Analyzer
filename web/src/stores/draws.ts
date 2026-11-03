/**
 * 开奖数据缓存（Pinia，仅内存）。
 *
 * 后端没有“最新一期”接口，`GET /draws` 只能从最早一期向后翻页；这里统一
 * 翻完当前数据并缓存，供总览、统计、候选与回测复用。刷新页面会重新读取，
 * 不写浏览器存储。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { fetchAllDraws, latestDraw, recentDraws, recentRange } from '../api/draws'
import { toApiError, type ApiError } from '../api/client'
import type { Draw } from '../api/types'

export const useDrawsStore = defineStore('draws', () => {
  const items = ref<Draw[]>([])
  const game = ref('ssq')
  const loading = ref(false)
  const error = ref<ApiError | null>(null)
  const loadedAt = ref<string | null>(null)

  let inflight: Promise<void> | null = null

  const latest = computed(() => latestDraw(items.value))
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

  function recent(size: number): Draw[] {
    return recentDraws(items.value, size)
  }

  function range(size: number): { start_issue: string; end_issue: string; count: number } | null {
    return recentRange(items.value, size)
  }

  function clear(): void {
    items.value = []
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
    recent,
    range,
    clear,
  }
})
