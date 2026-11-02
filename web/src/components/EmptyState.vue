<script setup lang="ts">
withDefaults(
  defineProps<{
    title: string
    description?: string
    hint?: string
  }>(),
  { description: '', hint: '' },
)
</script>

<template>
  <div class="empty-state">
    <div class="empty-state-head">
      <span class="empty-state-mark" aria-hidden="true">—</span>
      <h3>{{ title }}</h3>
    </div>
    <p v-if="description" class="empty-state-text">{{ description }}</p>
    <p v-if="hint" class="empty-hint">{{ hint }}</p>
    <slot />
  </div>
</template>
