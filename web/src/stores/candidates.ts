/**
 * 本次浏览器会话产生的候选批次（Pinia，仅内存）。
 *
 * 后端当前没有“列出我的候选批次”接口，因此候选记录页只能展示本会话通过
 * 候选任务或手动批次号读到的批次；刷新页面后列表为空，可重新输入批次号
 * 读取本人批次。这里不写 localStorage，也不缓存他人批次。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import type { CandidateBatch } from '../api/types'

export const useCandidatesStore = defineStore('candidates', () => {
  const batches = ref<CandidateBatch[]>([])

  const lockedCount = computed(
    () =>
      batches.value.filter((batch) =>
        ['locked', 'awaiting_result', 'settled'].includes(batch.status),
      ).length,
  )

  function remember(batch: CandidateBatch): void {
    const index = batches.value.findIndex((item) => item.id === batch.id)
    if (index >= 0) {
      batches.value[index] = batch
    } else {
      batches.value.push(batch)
    }
  }

  function find(id: number): CandidateBatch | undefined {
    return batches.value.find((batch) => batch.id === id)
  }

  function clear(): void {
    batches.value = []
  }

  return {
    batches,
    lockedCount,
    remember,
    find,
    clear,
  }
})
