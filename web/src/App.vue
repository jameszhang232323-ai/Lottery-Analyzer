<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import { useSystemStore } from './stores/system'

const route = useRoute()
const system = useSystemStore()

const statusLabel = computed(() => {
  switch (system.status) {
    case 'ok':
      return `后端正常 · ${system.games.length} 个彩种`
    case 'loading':
      return '正在检测后端…'
    case 'error':
      return `后端未连接：${system.lastError}`
    default:
      return '尚未连接后端'
  }
})

const navigation = [
  { to: '/', label: '总览' },
  { to: '/draws', label: '开奖数据' },
  { to: '/analysis', label: '统计分析' },
  { to: '/ai', label: 'AI 分析' },
  { to: '/strategies', label: '策略实验' },
  { to: '/backtests', label: '历史回测' },
  { to: '/candidates', label: '候选记录' },
  { to: '/settings', label: '设置与任务' },
]

onMounted(() => {
  void system.refresh()
})
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="brand">
        <span class="brand-name">大吉大利</span>
        <span class="brand-sub">福彩数据分析工作台</span>
      </div>
      <div class="global-status">
        <span class="status-dot" :data-status="system.status" aria-hidden="true"></span>
        <span class="status-text">{{ statusLabel }}</span>
        <button type="button" class="ghost-button" @click="system.refresh()">重新检测</button>
      </div>
    </header>

    <div class="app-body">
      <nav class="app-nav" aria-label="主导航">
        <RouterLink
          v-for="item in navigation"
          :key="item.to"
          :to="item.to"
          class="nav-link"
          :class="{ 'nav-link-active': route.path === item.to }"
        >
          {{ item.label }}
        </RouterLink>
      </nav>

      <main class="app-main">
        <RouterView />
      </main>
    </div>

    <footer class="app-footer">
      <span>研究工具，不构成购彩建议；不提供购票、代购、合买或充值功能。</span>
      <span>前端骨架 v0.1.0 · 数据尚未接入</span>
    </footer>
  </div>
</template>
