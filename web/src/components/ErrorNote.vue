<script setup lang="ts">
import { computed } from 'vue'

import type { ApiError } from '../api/client'
import StatusPill from './StatusPill.vue'

const props = withDefaults(
  defineProps<{
    error: ApiError | null
    /** 出错动作，例如“读取开奖数据”。 */
    context?: string
  }>(),
  { context: '' },
)

const heading = computed(() =>
  props.error
    ? `${props.context ? `${props.context}：` : ''}${props.error.kindLabel}`
    : '',
)

const detailEntries = computed(() =>
  props.error ? Object.entries(props.error.details).slice(0, 6) : [],
)
</script>

<template>
  <div v-if="error" class="error-note" role="alert">
    <div class="error-note-head">
      <StatusPill tone="error" :label="heading" />
      <code v-if="error.code" class="error-code">{{ error.code }}</code>
      <span v-if="error.status" class="muted">HTTP {{ error.status }}</span>
    </div>
    <p class="error-message">{{ error.message }}</p>
    <p v-if="error.requestId" class="muted">请求号：{{ error.requestId }}</p>
    <details v-if="detailEntries.length" class="error-details">
      <summary>错误详情</summary>
      <dl class="kv">
        <template v-for="[key, value] in detailEntries" :key="key">
          <dt>{{ key }}</dt>
          <dd>{{ typeof value === 'string' ? value : JSON.stringify(value) }}</dd>
        </template>
      </dl>
    </details>
    <p v-if="error.kind === 'network'" class="muted">
      请求可能未到达后端：数据没有被提交，可以确认服务后重试。
    </p>
    <slot />
  </div>
</template>
