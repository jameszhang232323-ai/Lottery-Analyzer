<script setup lang="ts">
import { computed } from 'vue'

import type { StatusTone } from '../stores/selectors'

const props = withDefaults(
  defineProps<{
    label: string
    tone?: StatusTone
    /** 可选图标覆盖；默认图标用于保证状态不只靠颜色区分。 */
    icon?: string
  }>(),
  { tone: 'neutral', icon: '' },
)

const DEFAULT_GLYPHS: Record<StatusTone, string> = {
  ok: '✓',
  warn: '!',
  error: '×',
  info: 'i',
  neutral: '·',
}

const glyph = computed(() => props.icon || DEFAULT_GLYPHS[props.tone])
</script>

<template>
  <span class="status-pill" :data-tone="tone">
    <span class="status-pill-glyph" aria-hidden="true">{{ glyph }}</span>
    <span class="status-pill-label">{{ label }}</span>
  </span>
</template>
