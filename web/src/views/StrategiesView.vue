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
import type {
  CandidateBatch,
  CandidateResponse,
  Snapshot,
  StrategyConfig,
  StrategyConditions,
  StrategyType,
  WindowSpec,
} from '../api/types'
import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorNote from '../components/ErrorNote.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
import { useJobLauncher } from '../composables/useJobLauncher'
import { useCandidatesStore } from '../stores/candidates'
import { useDrawsStore } from '../stores/draws'
import { useJobsStore } from '../stores/jobs'
import {
  describeCandidateStatus,
  describeJobStatus,
  formatCount,
  formatSyncTime,
} from '../stores/selectors'
import {
  isSeedHex,
  parseNumberList,
  parseOptionalInteger,
  parseOptionalNumberList,
  parseRangePair,
  parseTriple,
  randomSeedHex,
} from '../utils/numbers'

const draws = useDrawsStore()
const jobs = useJobsStore()
const candidates = useCandidatesStore()
const { launch } = useJobLauncher()
const route = useRoute()

const strategyType = ref<StrategyType>('S0')
const strategyWindow = ref<WindowSpec>(100)
const alphaPeriods = ref(30)
const lambda = ref(0.1)
const candidateCount = ref(5)
const seedHex = ref(randomSeedHex())
const targetIssue = ref('')

const fixedReds = ref('')
const excludedReds = ref('')
const useOddCounts = ref(false)
const oddCounts = ref('')
const useSumRange = ref(false)
const sumLow = ref('')
const sumHigh = ref('')
const useZoneCounts = ref(false)
const zoneCounts = ref('')
const useAdjacent = ref(false)
const maxAdjacent = ref('')

const snapshot = ref<Snapshot | null>(null)
const batch = ref<CandidateBatch | null>(null)
const batchInput = ref('')
const jobId = ref<number | null>(null)
const submitting = ref(false)
const loadingBatch = ref(false)
const error = ref<ApiError | null>(null)

const trackedJob = computed(() => (jobId.value === null ? null : jobs.find(jobId.value) ?? null))
const jobStatus = computed(() => (trackedJob.value ? describeJobStatus(trackedJob.value.status) : null))
const batchStatus = computed(() =>
  batch.value ? describeCandidateStatus(batch.value.status) : null,
)

const conditions = computed<StrategyConditions>(() => ({
  fixed_red: parseNumberList(fixedReds.value),
  excluded_red: parseNumberList(excludedReds.value),
  allowed_odd_counts: useOddCounts.value ? parseOptionalNumberList(oddCounts.value) : null,
  sum_range: useSumRange.value ? parseRangePair(sumLow.value, sumHigh.value) : null,
  zone_counts: useZoneCounts.value ? parseTriple(zoneCounts.value) : null,
  max_adjacent_pairs: useAdjacent.value ? parseOptionalInteger(maxAdjacent.value) : null,
}))

const strategyPayload = computed<StrategyConfig>(() => {
  if (strategyType.value === 'S0') {
    return {
      strategy_type: 'S0',
      strategy_version: 'v1',
      conditions: conditions.value,
    }
  }
  return {
    strategy_type: 'S1',
    strategy_version: 'v1',
    window: strategyWindow.value,
    alpha_periods: alphaPeriods.value,
    lam: lambda.value,
    conditions: conditions.value,
  }
})

function regenerateSeed(): void {
  seedHex.value = randomSeedHex()
}

async function ensureDraws(): Promise<boolean> {
  if (!draws.loadedAt) await draws.load()
  if (draws.error) {
    error.value = draws.error
    return false
  }
  if (draws.count === 0) {
    error.value = clientError('后端没有已取得的开奖记录，无法创建快照')
    return false
  }
  return true
}

async function submit(): Promise<void> {
  error.value = null
  if (!isSeedHex(seedHex.value)) {
    error.value = clientError('种子必须是 64 位十六进制字符串')
    return
  }
  if (!Number.isInteger(candidateCount.value) || candidateCount.value < 1 || candidateCount.value > 20) {
    error.value = clientError('候选组数必须是 1—20 的整数')
    return
  }
  if (fixedReds.value.trim() && conditions.value.fixed_red.length === 0) {
    error.value = clientError('固定红球格式不合法，请填写 1—33 的整数')
    return
  }
  if (excludedReds.value.trim() && conditions.value.excluded_red.length === 0) {
    error.value = clientError('排除红球格式不合法，请填写 1—33 的整数')
    return
  }
  if (useOddCounts.value && conditions.value.allowed_odd_counts === null) {
    error.value = clientError('已勾选“限制奇数个数”，请填写允许的奇数个数')
    return
  }
  if (useSumRange.value && conditions.value.sum_range === null) {
    error.value = clientError('已勾选“限制和值区间”，请填写完整的上下限')
    return
  }
  if (useZoneCounts.value && conditions.value.zone_counts === null) {
    error.value = clientError('已勾选“限制三区个数”，请填写恰好 3 个整数')
    return
  }
  if (useAdjacent.value && conditions.value.max_adjacent_pairs === null) {
    error.value = clientError('已勾选“限制最大连号对数”，请填写 0—5 的整数')
    return
  }
  submitting.value = true
  try {
    if (!(await ensureDraws())) return
    const requested =
      strategyWindow.value === 'all' ? draws.count : Math.max(300, Number(strategyWindow.value))
    const range = draws.range(Math.min(requested, draws.count))
    if (!range) throw clientError('没有可用的期号区间')

    snapshot.value = await apiPost<Snapshot>('/snapshots', {
      game: 'ssq',
      start_issue: range.start_issue,
      end_issue: range.end_issue,
    })

    const payload = await apiPost<CreateJobResponse>('/candidate-jobs', {
      snapshot_id: snapshot.value.snapshot_id,
      strategy: strategyPayload.value,
      count: candidateCount.value,
      seed_hex: seedHex.value.toLowerCase(),
      ...(targetIssue.value.trim() ? { target_issue: targetIssue.value.trim() } : {}),
    })
    jobId.value = payload.job_id
    launch({
      id: payload.job_id,
      kind: 'candidate.generate',
      label: `${strategyType.value} 候选生成`,
      status: payload.status,
    })
  } catch (caught) {
    error.value = toApiError(caught)
  } finally {
    submitting.value = false
  }
}

async function loadBatch(id: number): Promise<void> {
  loadingBatch.value = true
  error.value = null
  try {
    const payload = await apiGet<CandidateResponse>(`/candidates/${id}`)
    batch.value = payload.batch
    candidates.remember(payload.batch)
  } catch (caught) {
    error.value = toApiError(caught)
  } finally {
    loadingBatch.value = false
  }
}

async function loadBatchFromInput(): Promise<void> {
  const id = Number(batchInput.value.trim())
  if (!Number.isInteger(id) || id < 1) {
    error.value = clientError('批次号必须为正整数')
    return
  }
  await loadBatch(id)
}

watch(
  () => trackedJob.value?.updatedAt,
  () => {
    if (trackedJob.value?.status !== 'succeeded') return
    const result = trackedJob.value?.result
    if (result && typeof result === 'object' && 'batch_id' in result) {
      const id = Number((result as Record<string, unknown>).batch_id)
      if (Number.isInteger(id) && id > 0) void loadBatch(id)
    }
  },
)

onMounted(() => {
  if (!draws.loadedAt) void draws.load()
  const raw = route.query.job
  const value = Array.isArray(raw) ? raw[0] : raw
  const id = typeof value === 'string' ? Number(value) : Number.NaN
  if (Number.isInteger(id) && id > 0) jobId.value = id
})
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>策略实验</h1>
        <p class="view-subtitle">S0 均匀随机、S1 平滑频率、结构条件与真实候选表。</p>
      </div>
      <StatusPill
        :tone="jobStatus?.tone ?? (batch ? batchStatus?.tone ?? 'neutral' : 'neutral')"
        :label="jobStatus?.label ?? (batch ? batchStatus?.label ?? '候选已读取' : '尚未生成候选')"
      />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      S1 的抽样权重是候选生成权重，既不是号码的真实边际概率，也不是整组中奖概率；
      相同快照、引擎版本、参数、条件与种子必须得到完全相同的候选。S0 不接收 S1 参数。
    </p>

    <SectionCard title="策略参数" description="方法与参数白名单，S0 隐藏 S1 参数">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">方法</span>
          <select v-model="strategyType" class="field-input">
            <option value="S0">S0 均匀随机</option>
            <option value="S1">S1 平滑频率混合</option>
          </select>
        </label>
        <template v-if="strategyType === 'S1'">
          <label class="field">
            <span class="field-label">窗口</span>
            <select v-model="strategyWindow" class="field-input">
              <option :value="30">30 期</option>
              <option :value="100">100 期</option>
              <option :value="300">300 期</option>
              <option value="all">全范围</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">等效先验期数 α</span>
            <select v-model.number="alphaPeriods" class="field-input">
              <option :value="10">10</option>
              <option :value="30">30</option>
              <option :value="100">100</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">混合系数 λ</span>
            <select v-model.number="lambda" class="field-input">
              <option :value="0">0</option>
              <option :value="0.1">0.1</option>
              <option :value="0.25">0.25</option>
            </select>
          </label>
        </template>
        <label class="field">
          <span class="field-label">候选组数</span>
          <input v-model.number="candidateCount" class="field-input" type="number" min="1" max="20" />
        </label>
        <label class="field">
          <span class="field-label">目标期（可选）</span>
          <input v-model="targetIssue" class="field-input" type="text" inputmode="numeric" placeholder="留空由后端按日历取下一期" />
        </label>
      </div>
      <div class="data-status-grid">
        <DataStatus label="算法版本" value="sha256-counter-v1" />
        <DataStatus label="策略类型" :value="strategyPayload.strategy_type" />
        <DataStatus label="参数版本" value="v1" />
        <DataStatus label="批内去重" value="是" />
      </div>
      <label class="field">
        <span class="field-label">种子（64 位十六进制，仅用于复现）</span>
        <div class="inline-input-row">
          <input v-model="seedHex" class="field-input" type="text" spellcheck="false" />
          <button type="button" class="ghost-button" @click="regenerateSeed">重新生成</button>
        </div>
      </label>
      <p class="muted">种子不包含凭证；请保存种子与参数，才能复现同一批候选。</p>
    </SectionCard>

    <SectionCard title="结构条件" description="条件无解时保留后端错误码，不静默放宽" tag="可选" tag-tone="info">
      <div class="checkbox-row">
        <label><input v-model="useOddCounts" type="checkbox" /> 限制奇数个数</label>
        <label><input v-model="useSumRange" type="checkbox" /> 限制和值区间</label>
        <label><input v-model="useZoneCounts" type="checkbox" /> 限制三区个数</label>
        <label><input v-model="useAdjacent" type="checkbox" /> 限制最大连号对数</label>
      </div>
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">固定红球</span>
          <input v-model="fixedReds" class="field-input" type="text" placeholder="例如 01, 05, 09" />
        </label>
        <label class="field">
          <span class="field-label">排除红球</span>
          <input v-model="excludedReds" class="field-input" type="text" placeholder="例如 02, 33" />
        </label>
        <label v-if="useOddCounts" class="field">
          <span class="field-label">允许的奇数个数</span>
          <input v-model="oddCounts" class="field-input" type="text" placeholder="例如 2,3,4" />
        </label>
        <template v-if="useSumRange">
          <label class="field">
            <span class="field-label">和值下限</span>
            <input v-model="sumLow" class="field-input" type="number" inputmode="numeric" />
          </label>
          <label class="field">
            <span class="field-label">和值上限</span>
            <input v-model="sumHigh" class="field-input" type="number" inputmode="numeric" />
          </label>
        </template>
        <label v-if="useZoneCounts" class="field">
          <span class="field-label">三区个数（和必须为 6）</span>
          <input v-model="zoneCounts" class="field-input" type="text" placeholder="例如 2,2,2" />
        </label>
        <label v-if="useAdjacent" class="field">
          <span class="field-label">最大连号对数</span>
          <input v-model="maxAdjacent" class="field-input" type="number" min="0" max="5" />
        </label>
      </div>
      <p class="muted">
        固定红球不超过 6 个，固定与排除不得冲突，三区个数之和必须为 6；最终由后端做白名单与范围校验。
      </p>
    </SectionCard>

    <SectionCard title="快照与任务" description="先固定快照，再异步生成候选">
      <p class="muted">
        快照区间默认取已取得的最新 {{ formatCount(draws.count) }} 中足够覆盖所选窗口的期数；
        候选生成只读取快照内早于目标期的数据。
      </p>
      <div v-if="snapshot" class="data-status-grid">
        <DataStatus label="快照号" :value="snapshot.snapshot_id" />
        <DataStatus label="区间" :value="`${snapshot.start_issue}—${snapshot.end_issue}`" />
        <DataStatus label="成员" :value="formatCount(snapshot.member_count)" />
        <DataStatus label="哈希" :value="snapshot.hash.slice(0, 16)" />
      </div>
      <p v-if="trackedJob" class="muted">
        任务 #{{ trackedJob.id }}：{{ jobStatus?.label }} · {{ jobs.progressOf(trackedJob).text }}
        <template v-if="trackedJob.errorCode">
          · 错误码 <code>{{ trackedJob.errorCode }}</code>
          <template v-if="trackedJob.errorMessage">（{{ trackedJob.errorMessage }}）</template>
        </template>
        <template v-if="trackedJob.pollError">· 轮询：{{ trackedJob.pollError.message }}</template>
      </p>
      <ErrorNote :error="error" context="候选生成" />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" :disabled="submitting" @click="submit">
            {{ submitting ? '提交中…' : '创建快照并生成候选' }}
          </button>
          <label class="field analysis-inline-field">
            <span class="field-label">按批次号读取</span>
            <input v-model="batchInput" class="field-input" type="text" inputmode="numeric" placeholder="candidates id" />
          </label>
          <button type="button" class="ghost-button" :disabled="loadingBatch" @click="loadBatchFromInput">
            读取批次
          </button>
        </div>
      </template>
    </SectionCard>

    <SectionCard
      title="候选表"
      description="目标期、号码、种子与批次状态"
      :tag="batch ? `批次 #${batch.id}` : '未生成'"
      :tag-tone="batch ? batchStatus?.tone ?? 'neutral' : 'neutral'"
      wide
    >
      <p v-if="loadingBatch" class="loading-note">正在读取候选批次…</p>
      <template v-if="batch">
        <div class="data-status-grid">
          <DataStatus label="批次状态" :value="batchStatus?.label ?? batch.status" :tone="batchStatus?.tone ?? 'neutral'" />
          <DataStatus label="目标期" :value="batch.target_issue" />
          <DataStatus label="候选组数" :value="batch.candidate_count" />
          <DataStatus label="当前修订" :value="batch.revision" />
          <DataStatus label="种子" :value="batch.seed_hex.slice(0, 16)" :note="batch.seed_hex" />
          <DataStatus label="生成时间" :value="formatSyncTime(batch.created_at)" />
        </div>
        <div class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">组</th>
                <th scope="col">红球</th>
                <th scope="col">蓝球</th>
                <th scope="col">行哈希</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="line in batch.lines" :key="line.line_no">
                <td>{{ line.line_no }}</td>
                <td>
                  <span class="ball-row ball-row--compact">
                    <span v-for="red in line.reds" :key="`${line.line_no}-${red}`" class="ball ball--red">
                      {{ String(red).padStart(2, '0') }}
                    </span>
                  </span>
                </td>
                <td>
                  <span class="ball ball--blue">{{ String(line.blue).padStart(2, '0') }}</span>
                </td>
                <td>{{ line.line_hash.slice(0, 16) }}</td>
              </tr>
              <tr v-if="batch.lines.length === 0">
                <td class="table-empty" colspan="4">批次没有号码行</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <EmptyState
        v-else
        title="暂无候选批次"
        description="提交候选任务后，任务结果返回 batch_id，再读取真实号码行。"
        hint="不显示任何本地生成的号码；所有候选来自后端引擎。"
      />
      <template #actions>
        <RouterLink class="action-link" to="/candidates">前往候选记录</RouterLink>
        <RouterLink class="action-link" to="/backtests">加入回测</RouterLink>
      </template>
    </SectionCard>

    <SectionCard title="随机对照入口" description="B0 与 B1 由回测服务按同口径生成">
      <EmptyState
        title="随机对照在回测页提交"
        description="B0 为不加条件的均匀随机，B1 为施加相同条件的均匀随机；回测结果同时展示策略与两条基线。"
        hint="生成不足时保留实际产出数，不静默改变参数或重复补足。"
      />
      <template #actions>
        <RouterLink class="action-link" to="/backtests">配置回测与对照</RouterLink>
      </template>
    </SectionCard>
  </div>
</template>
