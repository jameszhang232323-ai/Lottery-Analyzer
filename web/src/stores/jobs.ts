/**
 * 全局任务中心状态（Pinia，仅内存）。
 *
 * 设计 11.3：耗时 POST 只入队并返回 job_id；前端每 2 秒轮询，终态停止。
 * `GET /jobs/{id}` 目前只返回 id/kind/status 等基础字段，因此对有专属详情
 * 接口的任务类型（统计、候选、回测）优先轮询专属接口，以取得 progress、
 * error_code 与 result；同步与 AI 任务没有专属详情接口，只能显示后端暴露的
 * 基础状态（该缺口在页面与交付报告中如实标注）。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { apiGet, clientError, toApiError, type ApiError } from '../api/client'
import type { JobDetailResponse, JobSummary } from '../api/types'
import { isTerminalJobStatus, jobKindLabel, describeJobProgress } from './selectors'

export const JOB_POLL_INTERVAL_MS = 2000

export interface TrackedJob {
  id: number
  kind: string
  label: string
  status: string
  progress: unknown
  checkpoint: unknown
  result: unknown
  errorCode: string | null
  errorMessage: string | null
  createdAt: string | null
  attempt: number | null
  pollError: ApiError | null
  updatedAt: string | null
}

function isKnownJobStatus(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    ['queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled', 'interrupted'].includes(value)
  )
}

function normalizeJob(job: JobSummary): JobSummary {
  return {
    ...job,
    status: isKnownJobStatus(job.status) ? job.status : 'interrupted',
  }
}

async function fetchJobDetail(id: number, kind: string): Promise<JobSummary> {
  if (kind === 'analysis.run') {
    const payload = await apiGet<JobDetailResponse>(`/analysis-jobs/${id}`)
    return payload.job
  }
  if (kind === 'candidate.generate') {
    const payload = await apiGet<JobDetailResponse>(`/candidate-jobs/${id}`)
    return payload.job
  }
  if (kind === 'backtest.run') {
    const payload = await apiGet<JobDetailResponse & { result?: unknown }>(`/backtest-jobs/${id}`)
    return payload.job
  }
  return apiGet<JobSummary>(`/jobs/${id}`)
}

export const useJobsStore = defineStore('jobs', () => {
  const jobs = ref<TrackedJob[]>([])
  const restoreError = ref<ApiError | null>(null)
  const polling = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null
  let refreshing = false

  const activeJobs = computed(() => jobs.value.filter((job) => !isTerminalJobStatus(job.status)))
  const runningCount = computed(
    () => jobs.value.filter((job) => job.status === 'running' || job.status === 'queued').length,
  )

  function find(id: number): TrackedJob | undefined {
    return jobs.value.find((job) => job.id === id)
  }

  function applyJob(tracked: TrackedJob, raw: JobSummary): void {
    const job = normalizeJob(raw)
    tracked.kind = job.kind || tracked.kind
    tracked.status = job.status
    tracked.progress = job.progress ?? job.checkpoint ?? tracked.progress
    tracked.checkpoint = job.checkpoint ?? tracked.checkpoint
    tracked.result = job.result ?? null
    tracked.errorCode = job.error_code ?? null
    tracked.errorMessage = job.error_message ?? null
    tracked.createdAt = job.created_at ?? tracked.createdAt
    tracked.attempt = typeof job.attempt === 'number' ? job.attempt : tracked.attempt
    tracked.pollError = null
    tracked.updatedAt = new Date().toISOString()
  }

  function hasActive(): boolean {
    return jobs.value.some((job) => !isTerminalJobStatus(job.status))
  }

  function stopTimer(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
    polling.value = false
  }

  function ensureTimer(): void {
    if (!hasActive()) {
      stopTimer()
      return
    }
    if (timer !== null) return
    polling.value = true
    timer = setInterval(() => {
      void refreshAll()
    }, JOB_POLL_INTERVAL_MS)
  }

  async function refresh(id: number): Promise<void> {
    const tracked = find(id)
    if (!tracked) return
    try {
      const raw = await fetchJobDetail(id, tracked.kind)
      applyJob(tracked, raw)
    } catch (caught) {
      tracked.pollError = toApiError(caught)
    } finally {
      ensureTimer()
    }
  }

  async function refreshAll(): Promise<void> {
    if (refreshing) return
    const targets = jobs.value.filter((job) => !isTerminalJobStatus(job.status))
    if (!targets.length) {
      stopTimer()
      return
    }
    refreshing = true
    try {
      await Promise.all(targets.map((job) => refresh(job.id)))
    } finally {
      refreshing = false
    }
  }

  interface TrackInput {
    id: number
    kind: string
    label?: string
    status?: string
  }

  function track(input: TrackInput): TrackedJob {
    const existing = find(input.id)
    if (existing) {
      if (input.label) existing.label = input.label
      if (input.kind) existing.kind = input.kind
      return existing
    }
    const tracked: TrackedJob = {
      id: input.id,
      kind: input.kind,
      label: input.label || jobKindLabel(input.kind),
      status: input.status || 'queued',
      progress: null,
      checkpoint: null,
      result: null,
      errorCode: null,
      errorMessage: null,
      createdAt: null,
      attempt: null,
      pollError: null,
      updatedAt: null,
    }
    jobs.value.push(tracked)
    void refresh(input.id)
    ensureTimer()
    return tracked
  }

  /** 页面刷新后按 job_id 恢复：先用通用端点取得 kind，再按类型轮询详情。 */
  async function restore(jobId: number): Promise<boolean> {
    restoreError.value = null
    if (!Number.isInteger(jobId) || jobId < 1) {
      restoreError.value = clientError('任务号必须为正整数')
      return false
    }
    if (find(jobId)) {
      await refresh(jobId)
      return true
    }
    try {
      const raw = await apiGet<JobSummary>(`/jobs/${jobId}`)
      const tracked = track({
        id: raw.id,
        kind: raw.kind,
        label: jobKindLabel(raw.kind),
        status: raw.status,
      })
      applyJob(tracked, raw)
      if (['analysis.run', 'candidate.generate', 'backtest.run'].includes(tracked.kind)) {
        await refresh(tracked.id)
      }
      return true
    } catch (caught) {
      restoreError.value = toApiError(caught)
      return false
    }
  }

  function clearFinished(): void {
    jobs.value = jobs.value.filter((job) => !isTerminalJobStatus(job.status))
    ensureTimer()
  }

  /** 退出登录/切换用户时清空全部任务与轮询。 */
  function clearAll(): void {
    jobs.value = []
    restoreError.value = null
    stopTimer()
  }

  function remove(id: number): void {
    jobs.value = jobs.value.filter((job) => job.id !== id)
    ensureTimer()
  }

  /** 组件卸载不应停止后台轮询；只有显式 clear 才停止。 */
  function progressOf(job: TrackedJob): { text: string; percent: number | null } {
    return describeJobProgress(job.progress, job.checkpoint)
  }

  return {
    jobs,
    activeJobs,
    runningCount,
    polling,
    restoreError,
    find,
    track,
    refresh,
    refreshAll,
    restore,
    clearFinished,
    clearAll,
    remove,
    progressOf,
  }
})
