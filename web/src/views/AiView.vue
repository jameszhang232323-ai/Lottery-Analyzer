<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import {
  apiGet,
  apiPost,
  clientError,
  toApiError,
  type ApiError,
  type CreateJobResponse,
} from '../api/client'
import type { AiRun, AiRunResponse, AiValidationIssue, Snapshot, WindowSpec } from '../api/types'
import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorNote from '../components/ErrorNote.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
import { useJobLauncher } from '../composables/useJobLauncher'
import { useDrawsStore } from '../stores/draws'
import { useJobsStore } from '../stores/jobs'
import { useSystemStore } from '../stores/system'
import {
  describeJobStatus,
  formatJsonValue,
  formatSyncTime,
  windowLabel,
} from '../stores/selectors'

const route = useRoute()
const draws = useDrawsStore()
const jobs = useJobsStore()
const system = useSystemStore()
const { launch } = useJobLauncher()

const WINDOW_OPTIONS: WindowSpec[] = [30, 100, 300, 'all']

const question = ref('请基于当前快照的统计事实，说明红球与蓝球的近期变化、结构特征和不确定性。')
const selectedWindows = ref<WindowSpec[]>([30, 100, 300, 'all'])
const allowedStrategies = ref<Array<'S0' | 'S1'>>(['S0', 'S1'])
const snapshotInput = ref('')
const snapshot = ref<Snapshot | null>(null)
const aiRun = ref<AiRun | null>(null)
const runInput = ref('')
const jobId = ref<number | null>(null)
const submitting = ref(false)
const runLoading = ref(false)
const error = ref<ApiError | null>(null)
const runError = ref<ApiError | null>(null)

const trackedJob = computed(() => (jobId.value === null ? null : jobs.find(jobId.value) ?? null))
const jobStatus = computed(() => (trackedJob.value ? describeJobStatus(trackedJob.value.status) : null))

const validationErrors = computed<AiValidationIssue[]>(() => aiRun.value?.validation.errors ?? [])
const report = computed(() => aiRun.value?.report ?? null)

const requestRecord = computed<Record<string, unknown> | null>(() => aiRun.value?.request ?? null)
const requestFacts = computed(() => {
  const value = requestRecord.value?.facts
  return Array.isArray(value) ? value.length : null
})
const requestWindows = computed(() => {
  const value = requestRecord.value?.windows
  return Array.isArray(value) ? value.map((item) => formatJsonValue(item)).join('、') : '—'
})

const outcomeLabel = computed(() => {
  switch (aiRun.value?.outcome) {
    case 'succeeded':
      return '成功'
    case 'invalid':
      return '校验未通过'
    case 'unknown':
      return '结果未知'
    case 'failed':
      return '失败'
    default:
      return aiRun.value ? aiRun.value.outcome : '未运行'
  }
})

const outcomeTone = computed<'ok' | 'warn' | 'error' | 'neutral'>(() => {
  switch (aiRun.value?.outcome) {
    case 'succeeded':
      return 'ok'
    case 'unknown':
      return 'warn'
    case 'invalid':
    case 'failed':
      return 'error'
    default:
      return 'neutral'
  }
})

function requestValue(key: string): unknown {
  return requestRecord.value ? requestRecord.value[key] : null
}

async function loadSnapshot(id: number): Promise<boolean> {
  try {
    snapshot.value = await apiGet<Snapshot>(`/snapshots/${id}`)
    snapshotInput.value = String(id)
    return true
  } catch (caught) {
    error.value = toApiError(caught)
    return false
  }
}

async function ensureSnapshot(): Promise<boolean> {
  const typed = Number(snapshotInput.value.trim())
  if (Number.isInteger(typed) && typed > 0) {
    return loadSnapshot(typed)
  }
  if (!draws.loadedAt) await draws.load()
  if (draws.error) {
    error.value = draws.error
    return false
  }
  if (draws.count === 0) {
    error.value = clientError('后端没有已取得的开奖记录，无法创建快照')
    return false
  }
  const numericWindows = selectedWindows.value.filter(
    (window): window is number => typeof window === 'number',
  )
  const requested = selectedWindows.value.includes('all')
    ? draws.count
    : Math.max(30, ...numericWindows)
  const range = draws.range(Math.min(requested, draws.count))
  if (!range) {
    error.value = clientError('没有可用的期号区间')
    return false
  }
  try {
    snapshot.value = await apiPost<Snapshot>('/snapshots', {
      game: 'ssq',
      start_issue: range.start_issue,
      end_issue: range.end_issue,
    })
    snapshotInput.value = String(snapshot.value.snapshot_id)
    return true
  } catch (caught) {
    error.value = toApiError(caught)
    return false
  }
}

async function submit(): Promise<void> {
  error.value = null
  runError.value = null
  if (!question.value.trim()) {
    error.value = clientError('研究问题不能为空')
    return
  }
  if (!selectedWindows.value.length) {
    error.value = clientError('至少选择一个统计窗口')
    return
  }
  if (!allowedStrategies.value.length) {
    error.value = clientError('至少允许一种策略')
    return
  }
  submitting.value = true
  try {
    if (!(await ensureSnapshot()) || !snapshot.value) return
    const payload = await apiPost<CreateJobResponse>('/ai-jobs', {
      snapshot_id: snapshot.value.snapshot_id,
      question: question.value.trim(),
      windows: selectedWindows.value,
      allowed_strategy_types: allowedStrategies.value,
    })
    jobId.value = payload.job_id
    launch({
      id: payload.job_id,
      kind: 'ai.analyze',
      label: 'AI 分析',
      status: payload.status,
    })
  } catch (caught) {
    error.value = toApiError(caught)
  } finally {
    submitting.value = false
  }
}

async function loadRun(id: number): Promise<void> {
  runLoading.value = true
  runError.value = null
  try {
    const payload = await apiGet<AiRunResponse>(`/ai-runs/${id}`)
    aiRun.value = payload.ai_run
  } catch (caught) {
    runError.value = toApiError(caught)
  } finally {
    runLoading.value = false
  }
}

async function loadRunFromInput(): Promise<void> {
  const id = Number(runInput.value.trim())
  if (!Number.isInteger(id) || id < 1) {
    error.value = clientError('AI 运行号必须为正整数')
    return
  }
  await loadRun(id)
}

watch(
  () => trackedJob.value?.updatedAt,
  () => {
    if (trackedJob.value?.status !== 'succeeded') return
    const result = trackedJob.value?.result
    if (result && typeof result === 'object' && 'ai_run_id' in result) {
      const id = Number((result as Record<string, unknown>).ai_run_id)
      if (Number.isInteger(id) && id > 0) void loadRun(id)
    }
  },
)

onMounted(() => {
  const value = route.query.snapshot
  const snapshotId = Array.isArray(value) ? value[0] : value
  if (typeof snapshotId === 'string' && /^\d+$/.test(snapshotId)) {
    snapshotInput.value = snapshotId
    void loadSnapshot(Number(snapshotId))
  }
  const rawJob = route.query.job
  const jobValue = Array.isArray(rawJob) ? rawJob[0] : rawJob
  const restoredJobId = typeof jobValue === 'string' ? Number(jobValue) : Number.NaN
  if (Number.isInteger(restoredJobId) && restoredJobId > 0) jobId.value = restoredJobId
  if (!draws.loadedAt) void draws.load()
})
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>AI 分析</h1>
        <p class="view-subtitle">研究问题、发送资料预览、AI 报告、事实引用与用量。</p>
      </div>
      <StatusPill
        :tone="system.aiStatus.tone"
        :label="system.aiStatus.label"
      />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      AI 只输出结构化建议，观察项必须引用后端事实 ID；号码、命中与回测结果直接引用引擎结果，
      不采信 AI 自行编写的数字。未经校验的建议不可应用。前端不接触服务密钥。
    </p>

    <SectionCard title="条件区" description="研究问题、快照与统计窗口">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">快照号（留空则按所选窗口取最近 N 期创建）</span>
          <input v-model="snapshotInput" class="field-input" type="text" inputmode="numeric" placeholder="snapshot_id" />
        </label>
        <label class="field">
          <span class="field-label">按运行号读取</span>
          <div class="inline-input-row">
            <input v-model="runInput" class="field-input" type="text" inputmode="numeric" placeholder="ai-runs id" />
            <button type="button" class="ghost-button" @click="loadRunFromInput">读取</button>
          </div>
        </label>
      </div>
      <div class="checkbox-row">
        <label v-for="window in WINDOW_OPTIONS" :key="String(window)">
          <input v-model="selectedWindows" type="checkbox" :value="window" />
          {{ windowLabel(window) }}
        </label>
      </div>
      <div class="checkbox-row">
        <label><input v-model="allowedStrategies" type="checkbox" value="S0" /> 允许 S0</label>
        <label><input v-model="allowedStrategies" type="checkbox" value="S1" /> 允许 S1</label>
      </div>
      <label class="field">
        <span class="field-label">研究问题</span>
        <textarea v-model="question" class="field-input" rows="3" maxlength="2000" />
      </label>
      <p class="muted">
        发送前会由后端在固定快照上重算事实；每日调用次数与费用上限由后端设置约束。
      </p>
      <div v-if="snapshot" class="data-status-grid">
        <DataStatus label="快照号" :value="snapshot.snapshot_id" />
        <DataStatus label="区间" :value="`${snapshot.start_issue}—${snapshot.end_issue}`" />
        <DataStatus label="成员" :value="snapshot.member_count" />
        <DataStatus label="快照哈希" :value="snapshot.hash.slice(0, 16)" />
      </div>
      <p v-if="trackedJob" class="muted">
        任务 #{{ trackedJob.id }}：{{ jobStatus?.label }} · {{ jobs.progressOf(trackedJob).text }}
        <template v-if="trackedJob.errorCode">
          · 错误码 <code>{{ trackedJob.errorCode }}</code>
          <template v-if="trackedJob.errorMessage">（{{ trackedJob.errorMessage }}）</template>
        </template>
      </p>
      <p v-if="trackedJob?.status === 'failed' && !trackedJob.errorCode" class="muted">
        后端 GET /jobs/{id} 目前只返回任务状态，没有 AI 作业错误详情；请在下方按 AI 运行号读取记录，
        或由管理员检查 AI 配置。
      </p>
      <p class="muted">
        后端当前没有任务取消端点，因此页面不提供“停止 AI 分析”按钮；已发出但丢失响应的收费调用不会自动重发。
      </p>
      <p v-if="runLoading" class="loading-note">正在读取 AI 运行记录…</p>
      <ErrorNote :error="error" context="提交 AI 任务" />
      <ErrorNote :error="runError" context="读取 AI 运行" />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" :disabled="submitting" @click="submit">
            {{ submitting ? '提交中…' : '发起 AI 分析' }}
          </button>
          <RouterLink class="action-link" to="/analysis">去统计分析创建快照</RouterLink>
        </div>
      </template>
    </SectionCard>

    <SectionCard
      title="发送资料预览"
      description="AnalysisEvidenceV1 的实际字段（来自 ai_run.request）"
      :tag="requestRecord ? '已读取' : '未读取'"
      :tag-tone="requestRecord ? 'ok' : 'neutral'"
      wide
    >
      <template v-if="requestRecord">
        <div class="data-status-grid">
          <DataStatus label="协议版本" :value="formatJsonValue(requestValue('schema_version'))" />
          <DataStatus label="截止期" :value="formatJsonValue(requestValue('as_of_issue'))" />
          <DataStatus label="有效样本" :value="formatJsonValue(requestValue('sample_n'))" />
          <DataStatus label="事实数量" :value="requestFacts ?? '—'" />
          <DataStatus label="窗口" :value="requestWindows" />
          <DataStatus label="快照哈希" :value="formatJsonValue(requestValue('snapshot_hash')).slice(0, 24)" />
        </div>
        <p class="muted">
          问题：{{ formatJsonValue(requestValue('question')) }}；允许策略：{{ formatJsonValue(requestValue('allowed_strategy_types')) }}。
        </p>
        <details class="raw-details">
          <summary>查看发送资料 JSON</summary>
          <pre>{{ JSON.stringify(requestRecord, null, 2) }}</pre>
        </details>
      </template>
      <EmptyState
        v-else
        title="没有可预览的资料包"
        description="任务成功或失败后，若后端保存了 ai_run，这里展示实际发送的证据包字段。"
        hint="不发送凭证、本地路径、完整数据库或未选择的个人备注。"
      />
    </SectionCard>

    <SectionCard
      title="AI 报告与引用"
      description="观察、策略建议、限制、校验结果与用量"
      :tag="outcomeLabel"
      :tag-tone="outcomeTone"
      wide
    >
      <div v-if="aiRun" class="data-status-grid">
        <DataStatus label="运行号" :value="aiRun.id" />
        <DataStatus label="结果" :value="outcomeLabel" :tone="outcomeTone" />
        <DataStatus label="提供方" :value="aiRun.provider" />
        <DataStatus label="模型" :value="aiRun.model" />
        <DataStatus label="提示词版本" :value="aiRun.prompt_version" />
        <DataStatus label="创建时间" :value="formatSyncTime(aiRun.created_at)" />
      </div>

      <ul v-if="validationErrors.length" class="plain-list">
        <li v-for="issue in validationErrors" :key="`${issue.code}-${issue.path}`">
          <code>{{ issue.code }}</code> · {{ issue.path }} · {{ issue.message }}
        </li>
      </ul>
      <p v-if="aiRun?.usage.error_code" class="muted">
        用量记录中的错误码：<code>{{ aiRun.usage.error_code }}</code>
      </p>

      <template v-if="report">
        <h3 class="subheading">观察（必须带 fact_id）</h3>
        <ul class="plain-list">
          <li v-for="(observation, index) in report.observations" :key="`obs-${index}`">
            {{ observation.interpretation }}
            <span class="muted">（引用：{{ observation.fact_ids.join('、') }}）</span>
          </li>
        </ul>
        <h3 class="subheading">策略建议（未经校验不可应用）</h3>
        <div class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">类型</th>
                <th scope="col">窗口</th>
                <th scope="col">α</th>
                <th scope="col">λ</th>
                <th scope="col">校验状态</th>
                <th scope="col">原因事实</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(proposal, index) in report.strategy_proposals" :key="`proposal-${index}`">
                <td>{{ proposal.strategy_type }}</td>
                <td>{{ proposal.window === null || proposal.window === undefined ? '—' : windowLabel(proposal.window) }}</td>
                <td>{{ proposal.alpha_periods ?? '—' }}</td>
                <td>{{ proposal.lambda ?? '—' }}</td>
                <td>{{ proposal.validation_status }}</td>
                <td>{{ proposal.reason_fact_ids?.join('、') || '—' }}</td>
              </tr>
              <tr v-if="report.strategy_proposals.length === 0">
                <td class="table-empty" colspan="6">没有策略建议</td>
              </tr>
            </tbody>
          </table>
        </div>
        <h3 class="subheading">限制说明</h3>
        <ul class="plain-list">
          <li v-for="(limitation, index) in report.limitations" :key="`limit-${index}`">{{ limitation }}</li>
        </ul>
      </template>
      <EmptyState
        v-else
        title="暂无 AI 报告"
        description="报告必须包含数据依据、历史观察、策略参数、引擎候选、对照证据与不确定性。"
        hint="AI 失败时保留后端已有结果；超时或丢响应标记未知，不自动重复计费调用。"
      />

      <h3 class="subheading">用量</h3>
      <div v-if="aiRun" class="data-status-grid">
        <DataStatus label="调用次数" :value="aiRun.usage.calls ?? '—'" />
        <DataStatus label="输入 tokens" :value="aiRun.usage.prompt_tokens ?? '—'" />
        <DataStatus label="输出 tokens" :value="aiRun.usage.completion_tokens ?? '—'" />
        <DataStatus label="总 tokens" :value="aiRun.usage.total_tokens ?? '—'" />
        <DataStatus label="耗时" :value="aiRun.usage.duration_ms === undefined ? '—' : `${aiRun.usage.duration_ms} ms`" />
        <DataStatus label="原始响应引用" :value="aiRun.usage.raw_ref ?? '—'" />
      </div>
      <p class="muted">
        用量与校验结果来自 /ai-runs/{{ aiRun?.id ?? '{id}' }}；失败时不会自动重发收费请求。
      </p>
    </SectionCard>

    <SectionCard title="使用边界" description="AI 可用性不影响 Python 统计与回测" wide>
      <ul class="plain-list">
        <li>浏览器不接触服务密钥，凭证只由后端读取并只返回掩码。</li>
        <li>AI 输出经过 Schema、事实引用与参数白名单校验后才可能被应用。</li>
        <li>AI 不可用时，统计、策略草稿与已生成候选照常使用。</li>
        <li>后端当前没有 AI 配置状态查询接口，页面显示“配置状态未知”而不猜测。</li>
      </ul>
    </SectionCard>
  </div>
</template>
