<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { describeJobStatus, formatSyncTime, jobKindLabel } from '../stores/selectors'
import { useJobsStore } from '../stores/jobs'
import StatusPill from './StatusPill.vue'

const jobs = useJobsStore()
const route = useRoute()

const open = ref(true)
const restoreInput = ref('')
const restoring = ref(false)

const activeCount = computed(() => jobs.activeJobs.length)
const runningLabel = computed(() =>
  activeCount.value > 0 ? `${activeCount.value} 个进行中` : '无进行中任务',
)

function progressFor(job: Parameters<typeof jobs.progressOf>[0]) {
  return jobs.progressOf(job)
}

async function restoreIds(ids: number[]): Promise<void> {
  if (!ids.length) return
  restoring.value = true
  for (const id of ids) await jobs.restore(id)
  restoring.value = false
}

function restoreFromInput(): void {
  const ids = restoreInput.value
    .split(/[\s,，]+/)
    .map((value) => Number(value.trim()))
    .filter((value) => Number.isInteger(value) && value > 0)
  void restoreIds(ids)
}

onMounted(() => {
  const raw = route.query.job
  const values = Array.isArray(raw) ? raw : [raw]
  const ids = values
    .flatMap((value) => String(value ?? '').split(','))
    .map((value) => Number(value.trim()))
    .filter((value) => Number.isInteger(value) && value > 0)
  void restoreIds(ids)
})
</script>

<template>
  <section class="task-center" aria-label="任务中心">
    <div class="task-center-bar">
      <div class="task-center-title">
        <strong>任务中心</strong>
        <StatusPill :tone="activeCount > 0 ? 'info' : 'neutral'" :label="runningLabel" />
        <span class="muted">运行中任务每 2 秒轮询后端；关闭页面不会取消任务。</span>
      </div>
      <div class="action-row">
        <button type="button" class="ghost-button" @click="open = !open">
          {{ open ? '收起任务' : '展开任务' }}
        </button>
        <button
          type="button"
          class="ghost-button"
          :disabled="jobs.jobs.every((job) => !['succeeded', 'failed', 'cancelled', 'interrupted'].includes(job.status))"
          @click="jobs.clearFinished()"
        >
          清除已结束
        </button>
      </div>
    </div>

    <div v-show="open" class="task-center-body">
      <form class="task-restore" @submit.prevent="restoreFromInput">
        <label class="field">
          <span class="field-label">按任务号恢复（页面刷新后可用）</span>
          <input
            v-model="restoreInput"
            class="field-input"
            type="text"
            inputmode="numeric"
            placeholder="例如 12 或 12,13"
          />
        </label>
        <button type="submit" class="action-button">恢复任务</button>
      </form>
      <p v-if="restoring" class="loading-note">正在按任务号恢复状态…</p>
      <p v-if="jobs.restoreError" class="error-inline" role="alert">
        恢复失败：{{ jobs.restoreError.kindLabel }} · {{ jobs.restoreError.code }} ·
        {{ jobs.restoreError.message }}
      </p>
      <p v-if="jobs.cancelError" class="error-inline" role="alert">
        取消失败：{{ jobs.cancelError.kindLabel }} · {{ jobs.cancelError.code }} ·
        {{ jobs.cancelError.message }}
      </p>
      <p v-if="jobs.stateError" class="error-inline" role="alert">
        状态操作失败：{{ jobs.stateError.kindLabel }} · {{ jobs.stateError.code }} ·
        {{ jobs.stateError.message }}
      </p>

      <p v-if="jobs.jobs.length === 0" class="muted task-empty">
        当前会话还没有任务。创建同步、统计、候选、回测或 AI 任务后会出现在这里。
      </p>

      <ul v-else class="task-list">
        <li v-for="job in jobs.jobs" :key="job.id" class="task-item">
          <div class="task-item-head">
            <span class="task-item-title">
              #{{ job.id }} · {{ job.label }}
              <span class="muted">（{{ jobKindLabel(job.kind) }}）</span>
            </span>
            <StatusPill
              :tone="describeJobStatus(job.status).tone"
              :label="describeJobStatus(job.status).label"
            />
            <button
              v-if="job.status === 'queued'"
              type="button"
              class="ghost-button"
              :disabled="jobs.isStateBusy(job.id)"
              @click="jobs.changeState(job.id, 'pause')"
            >
              {{ jobs.isStateBusy(job.id) ? '处理中…' : '暂停' }}
            </button>
            <button
              v-if="job.status === 'paused'"
              type="button"
              class="ghost-button"
              :disabled="jobs.isStateBusy(job.id)"
              @click="jobs.changeState(job.id, 'resume')"
            >
              {{ jobs.isStateBusy(job.id) ? '处理中…' : '恢复' }}
            </button>
            <button
              v-if="!['succeeded', 'failed', 'cancelled', 'interrupted'].includes(job.status)"
              type="button"
              class="ghost-button task-cancel"
              :disabled="jobs.isCancelling(job.id)"
              @click="jobs.cancel(job.id)"
            >
              {{ jobs.isCancelling(job.id) ? '取消中…' : '取消' }}
            </button>
            <button
              v-if="['succeeded', 'failed', 'cancelled', 'interrupted'].includes(job.status)"
              type="button"
              class="task-remove"
              aria-label="移除该任务"
              @click="jobs.remove(job.id)"
            >
              ×
            </button>
          </div>

          <div class="task-progress-row">
            <span class="task-progress-text">{{ progressFor(job).text }}</span>
            <span v-if="job.updatedAt" class="muted">更新于 {{ formatSyncTime(job.updatedAt) }}</span>
          </div>
          <div v-if="progressFor(job).percent !== null" class="task-progress" aria-hidden="true">
            <span :style="{ width: `${progressFor(job).percent}%` }" />
          </div>

          <p v-if="job.errorCode || job.errorMessage" class="task-error">
            错误码：<code>{{ job.errorCode || '—' }}</code>
            <template v-if="job.errorMessage"> · {{ job.errorMessage }}</template>
          </p>
          <p v-if="job.pollError" class="task-error">
            轮询失败：{{ job.pollError.kindLabel }} · {{ job.pollError.code }} ·
            {{ job.pollError.message }}
          </p>
        </li>
      </ul>
    </div>
  </section>
</template>
