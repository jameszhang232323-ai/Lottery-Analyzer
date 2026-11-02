<script setup lang="ts">
import { computed } from 'vue'

import type { StatusTone } from '../stores/selectors'
import StatusPill from './StatusPill.vue'

const props = withDefaults(
  defineProps<{
    label: string
    value?: string | number | null
    tone?: StatusTone
    /** 该字段尚无后端数据：显示“—”与“未接入”，不用 0 或空字符串冒充。 */
    unwired?: boolean
    note?: string
  }>(),
  { value: null, tone: 'neutral', unwired: false, note: '' },
)

const displayValue = computed(() => {
  if (props.value === null || props.value === undefined || props.value === '') return '—'
  return String(props.value)
})
</script>

<template>
  <div class="data-status" :data-tone="tone">
    <span class="data-status-label">{{ label }}</span>
    <span class="data-status-value">{{ displayValue }}</span>
    <StatusPill v-if="unwired" label="未接入" tone="neutral" />
    <span v-else-if="note" class="data-status-note">{{ note }}</span>
  </div>
</template>
