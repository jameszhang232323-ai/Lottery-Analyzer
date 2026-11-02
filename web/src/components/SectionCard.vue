<script setup lang="ts">
import { useSlots } from 'vue'

import type { StatusTone } from '../stores/selectors'
import StatusPill from './StatusPill.vue'

withDefaults(
  defineProps<{
    title: string
    description?: string
    /** 卡片右上角的短标签，例如“口径”“未接入”。 */
    tag?: string
    tagTone?: StatusTone
    /** 在自适应网格中占满整行，用于表格、时间线等宽内容。 */
    wide?: boolean
  }>(),
  { description: '', tag: '', tagTone: 'neutral', wide: false },
)

const slots = useSlots()
</script>

<template>
  <section class="section-card" :class="{ 'section-card--wide': wide }">
    <header class="section-card-header">
      <div class="section-card-heading">
        <h2>{{ title }}</h2>
        <p v-if="description" class="section-card-description">{{ description }}</p>
      </div>
      <StatusPill v-if="tag" :label="tag" :tone="tagTone" />
    </header>
    <div class="section-card-body">
      <slot />
    </div>
    <footer v-if="slots.actions" class="section-card-actions">
      <slot name="actions" />
    </footer>
  </section>
</template>
