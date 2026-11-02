<script setup lang="ts">
defineProps<{
  title: string
  description: string
  hint?: string
}>()
</script>

<template>
  <section class="empty-state">
    <h2>{{ title }}</h2>
    <p>{{ description }}</p>
    <p v-if="hint" class="empty-hint">{{ hint }}</p>
    <slot />
  </section>
</template>
