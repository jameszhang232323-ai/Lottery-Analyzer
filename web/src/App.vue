<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import DataStatus from './components/DataStatus.vue'
import EmptyState from './components/EmptyState.vue'
import StatusPill from './components/StatusPill.vue'
import { routes } from './router'
import { useSystemStore } from './stores/system'

const route = useRoute()
const system = useSystemStore()

/** 右侧 AI 解释区可折叠；窄屏（<1200px）默认折叠为浮层，不占工作区宽度。 */
const aiOpen = ref(
  typeof window === 'undefined' ? true : window.matchMedia('(min-width: 1200px)').matches,
)

const navigation = routes
  .filter((item) => item.meta?.nav)
  .sort((left, right) => (left.meta?.order ?? 0) - (right.meta?.order ?? 0))
  .map((item) => ({ path: item.path, label: item.meta?.nav ?? '' }))

const aiToggleLabel = computed(() => (aiOpen.value ? '收起 AI 解释' : '展开 AI 解释'))

const refreshLabel = computed(() =>
  system.connection === 'checking' ? '检测中…' : '重新检测',
)

onMounted(() => {
  void system.refresh()
})
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="header-top">
        <div class="brand">
          <span class="brand-name">大吉大利</span>
          <span class="brand-sub">福彩数据分析工作台</span>
        </div>
        <div class="header-actions">
          <button type="button" class="ghost-button" disabled title="后端接口未接入">
            同步数据
          </button>
          <span class="unwired-note">同步接口未接入</span>
          <button
            type="button"
            class="ghost-button"
            :disabled="system.connection === 'checking'"
            @click="system.refresh()"
          >
            {{ refreshLabel }}
          </button>
          <button
            type="button"
            class="ghost-button"
            :aria-expanded="aiOpen"
            aria-controls="ai-aside"
            @click="aiOpen = !aiOpen"
          >
            {{ aiToggleLabel }}
          </button>
        </div>
      </div>

      <div class="global-status-bar" role="group" aria-label="全局数据状态">
        <DataStatus
          v-for="field in system.globalStatus"
          :key="field.key"
          :label="field.label"
          :value="field.value"
          :tone="field.tone"
          :unwired="field.unwired"
          :note="field.note"
          :title="field.detail"
        />
      </div>
    </header>

    <div class="app-body" :data-ai="aiOpen ? 'open' : 'closed'">
      <nav class="app-nav" aria-label="主导航">
        <RouterLink
          v-for="item in navigation"
          :key="item.path"
          :to="item.path"
          class="nav-link"
          :class="{ 'nav-link-active': route.path === item.path }"
        >
          {{ item.label }}
        </RouterLink>
      </nav>

      <main class="app-main">
        <RouterView />
      </main>

      <aside v-show="aiOpen" id="ai-aside" class="ai-aside" aria-label="AI 解释区">
        <div class="ai-aside-header">
          <h2>AI 解释</h2>
          <StatusPill :tone="system.aiStatus.tone" :label="system.aiStatus.label" />
        </div>
        <p class="ai-aside-text">
          解释文本由后端 AI 生成并引用事实 ID；界面数字以事实集为准，不采信自由文本里的数字。
        </p>
        <EmptyState
          title="暂无 AI 解释"
          description="当前版本未接入 AI 服务，此处不展示任何生成内容。"
          hint="AI 不可用时，统计、策略草稿与已生成候选仍然保留。"
        />
        <p class="ai-aside-meta">{{ system.aiStatus.detail }}</p>
      </aside>
    </div>

    <footer class="app-footer">
      <span>研究工具，不构成购彩建议；不提供购票、代购、合买或充值功能。</span>
      <span>前端骨架 v0.1.0 · 仅健康探针与彩种清单已接通</span>
    </footer>
  </div>
</template>
