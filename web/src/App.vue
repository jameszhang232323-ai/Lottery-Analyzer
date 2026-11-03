<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { apiPost, toApiError, type ApiError, type CreateJobResponse } from './api/client'
import DataStatus from './components/DataStatus.vue'
import EmptyState from './components/EmptyState.vue'
import StatusPill from './components/StatusPill.vue'
import TaskCenter from './components/TaskCenter.vue'
import { useJobLauncher } from './composables/useJobLauncher'
import { routes } from './router'
import { useAuthStore } from './stores/auth'
import { useSystemStore } from './stores/system'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const system = useSystemStore()
const { launch } = useJobLauncher()

/** 右侧 AI 解释区可折叠；窄屏（<1200px）默认折叠为浮层，不占工作区宽度。 */
const aiOpen = ref(
  typeof window === 'undefined' ? true : window.matchMedia('(min-width: 1200px)').matches,
)

const syncing = ref(false)
const syncError = ref<ApiError | null>(null)
const logoutError = ref<ApiError | null>(null)

const isBlankLayout = computed(() => route.meta.layout === 'blank')
const isAdmin = computed(() => auth.isAdmin)
const syncGame = computed(() => system.enabledGames[0]?.code ?? 'ssq')

const navigation = routes
  .filter((item) => item.meta?.nav)
  .sort((left, right) => (left.meta?.order ?? 0) - (right.meta?.order ?? 0))
  .map((item) => ({ path: item.path, label: item.meta?.nav ?? '' }))

const aiToggleLabel = computed(() => (aiOpen.value ? '收起 AI 解释' : '展开 AI 解释'))

const refreshLabel = computed(() =>
  system.connection === 'checking' ? '检测中…' : '重新检测',
)

async function refreshAll(force = false): Promise<void> {
  await system.refreshAll('ssq', force)
}

async function runSync(): Promise<void> {
  syncError.value = null
  if (!isAdmin.value) return
  syncing.value = true
  try {
    const payload = await apiPost<CreateJobResponse>('/sync-jobs', {
      game: syncGame.value,
      mode: 'incremental',
    })
    launch({
      id: payload.job_id,
      kind: 'sync.run',
      label: `${syncGame.value} 增量同步`,
      status: payload.status,
    })
  } catch (caught) {
    syncError.value = toApiError(caught)
  } finally {
    syncing.value = false
  }
}

async function logout(): Promise<void> {
  logoutError.value = null
  const ok = await auth.logout()
  if (ok) {
    await router.replace({ name: 'login' })
  } else {
    logoutError.value = auth.error
  }
}

watch(
  () => auth.isAuthenticated,
  (value) => {
    if (value) void refreshAll()
  },
)

onMounted(() => {
  if (auth.isAuthenticated) void refreshAll()
})
</script>

<template>
  <RouterView v-if="isBlankLayout" />

  <div v-else class="app-shell">
    <header class="app-header">
      <div class="header-top">
        <div class="brand">
          <span class="brand-name">大吉大利</span>
          <span class="brand-sub">福彩数据分析工作台</span>
        </div>
        <div class="header-actions">
          <button
            type="button"
            class="ghost-button"
            :disabled="syncing || !isAdmin"
            :title="isAdmin ? '以增量模式创建同步任务' : '需要管理员角色'"
            @click="runSync"
          >
            {{ syncing ? '提交中…' : '同步数据' }}
          </button>
          <span v-if="!isAdmin" class="unwired-note">仅管理员可同步</span>
          <button
            type="button"
            class="ghost-button"
            :disabled="system.connection === 'checking'"
            @click="refreshAll(true)"
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
          <div class="user-chip">
            <span class="user-name">{{ auth.displayName }}</span>
            <span class="user-role">{{ auth.role }}</span>
            <button type="button" class="ghost-button" @click="logout">退出</button>
          </div>
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
      <p v-if="syncError" class="error-inline" role="alert">
        同步任务提交失败：{{ syncError.kindLabel }} · {{ syncError.code }} · {{ syncError.message }}
      </p>
      <p v-if="logoutError" class="error-inline" role="alert">
        退出失败：{{ logoutError.kindLabel }} · {{ logoutError.code }} · {{ logoutError.message }}
      </p>
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
          AI 报告在“AI 分析”页查看；观察项必须引用后端事实 ID，界面数字以事实集为准，
          不采信自由文本里的数字。
        </p>
        <EmptyState
          title="此处不展示生成内容"
          description="右侧只显示服务状态与引用规则，完整报告、校验结果与用量在 AI 分析页。"
          hint="AI 不可用时，统计、策略草稿与已生成候选仍然保留。"
        />
        <p class="ai-aside-meta">{{ system.aiStatus.detail }}</p>
      </aside>
    </div>

    <TaskCenter />

    <footer class="app-footer">
      <span>研究工具，不构成购彩建议；不提供购票、代购、合买或充值功能。</span>
      <span>前端 v0.1.0 · 八页已接真实接口</span>
    </footer>
  </div>
</template>
