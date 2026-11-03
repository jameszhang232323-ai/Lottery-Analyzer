<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  apiDownload,
  apiGet,
  apiPost,
  clientError,
  toApiError,
  type ApiError,
  type CreateJobResponse,
} from '../api/client'
import type {
  Backtest,
  BacktestResponse,
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
import { useDrawsStore } from '../stores/draws'
import { useJobsStore } from '../stores/jobs'
import {
  describeJobStatus,
  formatCount,
  formatJsonValue,  formatSyncTime,
} from '../stores/selectors'
import {
  isSeedHex,
  parseNumberList,
  randomSeedHex,
} from '../utils/numbers'
import { saveBlob } from '../utils/download'

const draws = useDrawsStore()
const jobs = useJobsStore()
const { launch } = useJobLauncher()
const route = useRoute()

const strategyType = ref<StrategyType>('S0')
const strategyWindow = ref<WindowSpec>(100)
const alphaPeriods = ref(30)
const lambda = ref(0.1)
const k = ref(5)
const trajectories = ref(200)
const stage = ref<'train' | 'validation' | 'holdout'>('validation')
const name = ref('')
const masterSeed = ref('')

const fixedReds = ref('')
const excludedReds = ref('')

const snapshot = ref<Snapshot | null>(null)
const backtest = ref<Backtest | null>(null)
const backtestInput = ref('')
const jobId = ref<number | null>(null)
const submitting = ref(false)
const loadingResult = ref(false)
const error = ref<ApiError | null>(null)
const exportLoading = ref(false)
const exportError = ref<ApiError | null>(null)

const trackedJob = computed(() => (jobId.value === null ? null : jobs.find(jobId.value) ?? null))
const jobStatus = computed(() => (trackedJob.value ? describeJobStatus(trackedJob.value.status) : null))
const resultStatus = computed(() => (backtest.value ? backtest.value.status : null))

const conditions = computed<StrategyConditions>(() => ({
  fixed_red: parseNumberList(fixedReds.value),
  excluded_red: parseNumberList(excludedReds.value),
  allowed_odd_counts: null,
  sum_range: null,
  zone_counts: null,
  max_adjacent_pairs: null,
}))

const strategyPayload = computed<StrategyConfig>(() => {
  if (strategyType.value === 'S0') {
    return { strategy_type: 'S0', strategy_version: 'v1', conditions: conditions.value }
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

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

const metrics = computed(() => asRecord(backtest.value?.metrics))
const strategySummary = computed(() => asRecord(metrics.value?.strategy))
const baselines = computed(() => asRecord(metrics.value?.baselines))
const b0Summary = computed(() => asRecord(asRecord(baselines.value?.b0)?.summary))
const b1Summary = computed(() => asRecord(asRecord(baselines.value?.b1)?.summary))
const comparisons = computed(() => asRecord(metrics.value?.comparisons))
const vsB0 = computed(() => asRecord(comparisons.value?.vs_b0))
const vsB1 = computed(() => asRecord(comparisons.value?.vs_b1))
const bootstrap = computed(() => asRecord(backtest.value?.bootstrap))
const bootstrapB0 = computed(() => asRecord(bootstrap.value?.vs_b0))
const bootstrapB1 = computed(() => asRecord(bootstrap.value?.vs_b1))
const plan = computed(() => backtest.value?.plan ?? null)

const summaryRows = computed(() => [
  { label: '策略', summary: strategySummary.value },
  { label: 'B0 均匀随机', summary: b0Summary.value },
  { label: 'B1 同条件随机', summary: b1Summary.value },
])

function regenerateSeed(): void {
  masterSeed.value = randomSeedHex()
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
  if (masterSeed.value.trim() && !isSeedHex(masterSeed.value.trim())) {
    error.value = clientError('主种子必须为空或 64 位十六进制字符串')
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
  if (k.value < 1 || k.value > 20) {
    error.value = clientError('K 必须在 1—20 之间')
    return
  }
  if (!Number.isInteger(k.value)) {
    error.value = clientError('K 必须是整数')
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

    const payload = await apiPost<CreateJobResponse>('/backtest-jobs', {
      snapshot_id: snapshot.value.snapshot_id,
      strategy: strategyPayload.value,
      k: k.value,
      attempts: [],
      baseline_trajectories: trajectories.value,
      stage: stage.value,
      ...(name.value.trim() ? { name: name.value.trim() } : {}),
      ...(masterSeed.value.trim() ? { master_seed_hex: masterSeed.value.trim().toLowerCase() } : {}),
    })
    jobId.value = payload.job_id
    launch({
      id: payload.job_id,
      kind: 'backtest.run',
      label: name.value.trim() || `${strategyType.value} 回测`,
      status: payload.status,
    })
  } catch (caught) {
    error.value = toApiError(caught)
  } finally {
    submitting.value = false
  }
}

async function loadBacktest(id: number): Promise<void> {
  loadingResult.value = true
  error.value = null
  try {
    const payload = await apiGet<BacktestResponse>(`/backtests/${id}`)
    backtest.value = payload.backtest
  } catch (caught) {
    error.value = toApiError(caught)
  } finally {
    loadingResult.value = false
  }
}

/** 导出当前回测结果：CSV 逐期明细，JSON 完整计划与指标。 */
async function exportBacktest(format: 'csv' | 'json'): Promise<void> {
  if (!backtest.value) return
  exportError.value = null
  exportLoading.value = true
  try {
    const file = await apiDownload(`/backtests/${backtest.value.id}/export?format=${format}`)
    saveBlob(file.blob, file.filename)
  } catch (caught) {
    exportError.value = toApiError(caught)
  } finally {
    exportLoading.value = false
  }
}

async function loadBacktestFromInput(): Promise<void> {
  const id = Number(backtestInput.value.trim())
  if (!Number.isInteger(id) || id < 1) {
    error.value = clientError('回测结果号必须为正整数')
    return
  }
  await loadBacktest(id)
}

watch(
  () => trackedJob.value?.updatedAt,
  () => {
    if (trackedJob.value?.status !== 'succeeded') return
    const result = trackedJob.value?.result
    if (result && typeof result === 'object' && 'backtest_id' in result) {
      const id = Number((result as Record<string, unknown>).backtest_id)
      if (Number.isInteger(id) && id > 0) void loadBacktest(id)
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
        <h1>历史回测</h1>
        <p class="view-subtitle">时间划分、参数冻结、任务进度、B0/B1 对照与逐期结果。</p>
      </div>
      <StatusPill
        :tone="jobStatus?.tone ?? (backtest ? 'ok' : 'neutral')"
        :label="jobStatus?.label ?? (backtest ? `结果 ${resultStatus}` : '尚未运行回测')"
      />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      按日期升序取最早 60% 训练、随后 20% 验证、最后 20% 封存，边界按完整开奖期取整；
      t 期候选只读取序号小于 t 的输入。主指标为每组平均红球命中数，区间含 0 时结论为证据不足。
    </p>

    <SectionCard title="时间划分与参数" description="首版固定 60/20/20；轨迹数可降档">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">方法</span>
          <select v-model="strategyType" class="field-input">
            <option value="S0">S0 均匀随机</option>
            <option value="S1">S1 平滑频率混合</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">K（每组候选）</span>
          <input v-model.number="k" class="field-input" type="number" min="1" max="20" />
        </label>
        <label class="field">
          <span class="field-label">B0/B1 轨迹数</span>
          <select v-model.number="trajectories" class="field-input">
            <option :value="50">50（降档）</option>
            <option :value="100">100</option>
            <option :value="200">200</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">评价阶段</span>
          <select v-model="stage" class="field-input">
            <option value="train">训练</option>
            <option value="validation">验证</option>
            <option value="holdout">封存</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">计划名称（可选）</span>
          <input v-model="name" class="field-input" type="text" maxlength="128" />
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
            <span class="field-label">α</span>
            <select v-model.number="alphaPeriods" class="field-input">
              <option :value="10">10</option>
              <option :value="30">30</option>
              <option :value="100">100</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">λ</span>
            <select v-model.number="lambda" class="field-input">
              <option :value="0">0</option>
              <option :value="0.1">0.1</option>
              <option :value="0.25">0.25</option>
            </select>
          </label>
        </template>
      </div>
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">固定红球</span>
          <input v-model="fixedReds" class="field-input" type="text" placeholder="例如 01, 05" />
        </label>
        <label class="field">
          <span class="field-label">排除红球</span>
          <input v-model="excludedReds" class="field-input" type="text" placeholder="例如 02, 33" />
        </label>
      </div>
      <label class="field">
        <span class="field-label">主种子（可选，留空由后端派生；仅用于复现）</span>
        <div class="inline-input-row">
          <input v-model="masterSeed" class="field-input" type="text" spellcheck="false" placeholder="64 位十六进制" />
          <button type="button" class="ghost-button" @click="regenerateSeed">生成</button>
        </div>
      </label>
      <div class="data-status-grid">
        <DataStatus label="训练段" value="60%" />
        <DataStatus label="验证段" value="20%" />
        <DataStatus label="封存段" value="20%" />
        <DataStatus label="主指标" value="每组平均红球命中数" />
      </div>
    </SectionCard>

    <SectionCard title="快照与任务" description="提交 202 后由任务中心轮询 /backtest-jobs/{id}">
      <p class="muted">
        快照默认取已取得的最新 {{ formatCount(draws.count) }} 中覆盖所选窗口的期数；
        回测由后端 worker 分批计算，关闭页面不停止任务。
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
      </p>
      <p class="muted">
        后端当前没有任务取消端点（POST /jobs/{id}/cancel 未实现），因此页面不提供“取消回测”按钮；
        关闭页面不会停止后端任务。
      </p>
      <ErrorNote :error="error" context="回测" />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" :disabled="submitting" @click="submit">
            {{ submitting ? '提交中…' : '创建快照并运行回测' }}
          </button>
          <label class="field analysis-inline-field">
            <span class="field-label">按结果号读取</span>
            <input v-model="backtestInput" class="field-input" type="text" inputmode="numeric" placeholder="backtests id" />
          </label>
          <button type="button" class="ghost-button" :disabled="loadingResult" @click="loadBacktestFromInput">
            读取结果
          </button>
        </div>
      </template>
    </SectionCard>

    <template v-if="backtest">
      <SectionCard
        title="计划与冻结记录"
        description="计划哈希、快照、划分摘要与封存状态"
        :tag="plan?.holdout_state ?? '未知'"
        tag-tone="info"
        wide
      >
        <div class="data-status-grid">
          <DataStatus label="回测号" :value="backtest.id" />
          <DataStatus label="结果状态" :value="backtest.status" />
          <DataStatus label="计划号" :value="backtest.plan_id" />
          <DataStatus label="计划哈希" :value="plan ? plan.plan_hash.slice(0, 16) : '—'" />
          <DataStatus label="主指标" :value="plan?.primary_metric ?? '—'" />
          <DataStatus label="开始" :value="formatSyncTime(backtest.started_at)" />
          <DataStatus label="结束" :value="formatSyncTime(backtest.finished_at)" />
          <DataStatus label="可评价/排除" :value="`${backtest.counts.evaluated} / ${backtest.counts.excluded}`" />
        </div>
        <details v-if="plan" class="raw-details">
          <summary>查看冻结计划</summary>
          <pre>{{ JSON.stringify(plan, null, 2) }}</pre>
        </details>
      </SectionCard>

      <SectionCard title="比较表" description="策略与 B0 / B1 的同口径汇总" wide>
        <div class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">组</th>
                <th scope="col">期数</th>
                <th scope="col">候选行</th>
                <th scope="col">平均红球命中</th>
                <th scope="col">蓝球命中率</th>
                <th scope="col">最大红球命中</th>
                <th scope="col">样本不足/空期</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in summaryRows" :key="row.label">
                <td>{{ row.label }}</td>
                <td>{{ formatJsonValue(row.summary?.periods) }}</td>
                <td>{{ formatJsonValue(row.summary?.lines) }}</td>
                <td>{{ formatJsonValue(row.summary?.red_hits_per_group) }}</td>
                <td>{{ formatJsonValue(row.summary?.blue_hit_rate) }}</td>
                <td>{{ formatJsonValue(row.summary?.max_red_hits) }}</td>
                <td>
                  {{ formatJsonValue(row.summary?.insufficient_periods) }} /
                  {{ formatJsonValue(row.summary?.empty_periods) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="data-status-grid">
          <DataStatus label="对照轨迹数" :value="formatJsonValue(baselines?.trajectory_count)" />
          <DataStatus
            label="条件等价"
            :value="baselines?.conditions_equivalent === true ? '是' : baselines?.conditions_equivalent === false ? '否' : '—'"
          />
          <DataStatus label="策略 vs B0" :value="formatJsonValue(vsB0?.point_diff)" />
          <DataStatus label="策略 vs B1" :value="formatJsonValue(vsB1?.point_diff)" />
        </div>
      </SectionCard>

      <SectionCard title="Bootstrap 区间" description="含 0 或不可靠时结论为证据不足" wide>
        <div class="data-status-grid">
          <DataStatus label="方法" :value="formatJsonValue(bootstrap?.method)" />
          <DataStatus label="置信度" :value="formatJsonValue(bootstrap?.confidence)" />
          <DataStatus label="重采样次数" :value="formatJsonValue(bootstrap?.resamples)" />
          <DataStatus label="块长" :value="formatJsonValue(bootstrap?.block_length)" />
        </div>
        <div class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">对照</th>
                <th scope="col">点估计</th>
                <th scope="col">下界</th>
                <th scope="col">上界</th>
                <th scope="col">含 0</th>
                <th scope="col">可靠</th>
                <th scope="col">证据</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>vs B0</td>
                <td>{{ formatJsonValue(bootstrapB0?.point) }}</td>
                <td>{{ formatJsonValue(bootstrapB0?.lower) }}</td>
                <td>{{ formatJsonValue(bootstrapB0?.upper) }}</td>
                <td>{{ bootstrapB0?.includes_zero === true ? '是' : bootstrapB0?.includes_zero === false ? '否' : '—' }}</td>
                <td>{{ bootstrapB0?.reliable === true ? '是' : bootstrapB0?.reliable === false ? '否' : '—' }}</td>
                <td>{{ formatJsonValue(bootstrapB0?.evidence) }}</td>
              </tr>
              <tr>
                <td>vs B1</td>
                <td>{{ formatJsonValue(bootstrapB1?.point) }}</td>
                <td>{{ formatJsonValue(bootstrapB1?.lower) }}</td>
                <td>{{ formatJsonValue(bootstrapB1?.upper) }}</td>
                <td>{{ bootstrapB1?.includes_zero === true ? '是' : bootstrapB1?.includes_zero === false ? '否' : '—' }}</td>
                <td>{{ bootstrapB1?.reliable === true ? '是' : bootstrapB1?.reliable === false ? '否' : '—' }}</td>
                <td>{{ formatJsonValue(bootstrapB1?.evidence) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionCard>

      <SectionCard title="逐期结果" description="候选哈希、命中明细、基线与排除原因" wide>        <div class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">期号</th>
                <th scope="col">阶段</th>
                <th scope="col">候选哈希</th>
                <th scope="col">命中明细</th>
                <th scope="col">基线</th>
                <th scope="col">排除原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="period in backtest.periods" :key="`${period.issue}-${period.candidate_hash}`">
                <td>{{ period.issue }}</td>
                <td>{{ period.stage }}</td>
                <td>{{ period.candidate_hash.slice(0, 16) }}</td>
                <td><code class="cell-code">{{ formatJsonValue(period.hits) }}</code></td>
                <td><code class="cell-code">{{ formatJsonValue(period.baseline) }}</code></td>
                <td>{{ period.excluded_reason || '—' }}</td>
              </tr>
              <tr v-if="backtest.periods.length === 0">
                <td class="table-empty" colspan="6">没有逐期结果</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="muted">
          共 {{ backtest.counts.periods }} 期，可评价 {{ backtest.counts.evaluated }} 期，排除
          {{ backtest.counts.excluded }} 期；排除期不混入完整报告。
        </p>
        <ErrorNote :error="exportError" context="导出回测结果" />
        <template #actions>
          <div class="action-row">
            <button
              type="button"
              class="action-button"
              :disabled="exportLoading"
              @click="exportBacktest('csv')"
            >
              {{ exportLoading ? '导出中…' : '导出 CSV' }}
            </button>
            <button
              type="button"
              class="ghost-button"
              :disabled="exportLoading"
              @click="exportBacktest('json')"
            >
              导出 JSON
            </button>
          </div>
        </template>
      </SectionCard>
    </template>

    <SectionCard v-else title="回测结果" description="任务成功后按 backtest_id 读取 /backtests/{id}" wide>
      <EmptyState
        title="暂无回测结果"
        description="创建快照并运行回测；任务成功后自动读取指标、B0/B1、Bootstrap 区间与逐期摘要。"
        hint="导出 CSV 为逐期明细，JSON 含完整计划、指标与区间。"
      />
      <p class="muted">最近读取时间：{{ formatSyncTime(draws.loadedAt) }}。</p>
    </SectionCard>
  </div>
</template>
