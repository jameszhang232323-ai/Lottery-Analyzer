<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import type { EChartsCoreOption } from 'echarts/core'

import { apiGet, apiPost, clientError, toApiError, type ApiError, type CreateJobResponse } from '../api/client'
import type {
  Analysis,
  AnalysisResponse,
  Fact,
  Snapshot,
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
  formatRatio,
  formatSyncTime,
  windowLabel,
} from '../stores/selectors'

/** 图表库按需加载，避免统计页以外的工作区为 ECharts 付出首屏体积。 */
const EChart = defineAsyncComponent(() => import('../components/EChart.vue'))

const draws = useDrawsStore()
const jobs = useJobsStore()
const { launch } = useJobLauncher()
const route = useRoute()

const WINDOW_OPTIONS: WindowSpec[] = [30, 100, 300, 'all']

const selectedWindows = ref<WindowSpec[]>([30, 100, 300, 'all'])
const selectedWindow = ref<WindowSpec>(100)
const snapshot = ref<Snapshot | null>(null)
const analysis = ref<Analysis | null>(null)
const analysisInput = ref('')
const jobId = ref<number | null>(null)
const submitting = ref(false)
const resultLoading = ref(false)
const loadError = ref<ApiError | null>(null)

const trackedJob = computed(() => (jobId.value === null ? null : jobs.find(jobId.value) ?? null))
const jobStatus = computed(() => (trackedJob.value ? describeJobStatus(trackedJob.value.status) : null))
const jobError = computed(() => trackedJob.value?.pollError ?? null)

const windowFacts = computed(() =>
  analysis.value ? analysis.value.facts.filter((fact) => fact.window === selectedWindow.value) : [],
)

const redFrequencyFacts = computed(() => filterFacts('ssq.red.', '.frequency.'))
const redOmissionFacts = computed(() => filterFacts('ssq.red.', '.omission.'))
const blueFrequencyFacts = computed(() => filterFacts('ssq.blue.', '.frequency.'))
const structureFacts = computed(() =>
  windowFacts.value.filter((fact) => /(sum|span|odd|big|zone|adjacent|repeat)\./.test(fact.fact_id)),
)

const blueOmissionFacts = computed(() => filterFacts('ssq.blue.', '.omission.'))

function filterFacts(prefix: string, metric: string): Fact[] {
  return windowFacts.value.filter(
    (fact) => fact.fact_id.startsWith(prefix) && fact.fact_id.includes(metric),
  )
}

function numberFromFact(fact: Fact): number {
  const parts = fact.fact_id.split('.')
  const value = Number(parts[2])
  return Number.isInteger(value) ? value : 0
}

function chartOption(
  facts: Fact[],
  title: string,
  color: string,
  unitLabel: string,
): EChartsCoreOption {
  const byNumber = new Map<number, Fact>()
  for (const fact of facts) {
    const number = numberFromFact(fact)
    if (number >= 1) byNumber.set(number, fact)
  }
  const maxNumber = facts.reduce((max, fact) => Math.max(max, numberFromFact(fact)), 0)
  const labels = Array.from({ length: maxNumber }, (_, index) => String(index + 1))
  const values = labels.map((label) => {
    const fact = byNumber.get(Number(label))
    return fact ? fact.value : null
  })
  return {
    title: { text: title, left: 'center', textStyle: { fontSize: 13, fontWeight: 600 } },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value: unknown) => {
        if (typeof value !== 'number') return `— ${unitLabel}`
        return `${unitLabel === '期频率' ? value.toFixed(4) : String(value)} ${unitLabel}`
      },
    },
    grid: { left: 40, right: 16, top: 40, bottom: 28 },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: { interval: labels.length > 20 ? 1 : 0, fontSize: 10 },
    },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    series: [
      {
        type: 'bar',
        data: values,
        itemStyle: { color },
        barMaxWidth: 14,
      },
    ],
  }
}

const redFrequencyOption = computed(() =>
  chartOption(redFrequencyFacts.value, '红球期频率（当前窗口）', '#b3261e', '期频率'),
)
const redOmissionOption = computed(() =>
  chartOption(redOmissionFacts.value, '红球当前遗漏（当前窗口）', '#a15c00', '期'),
)
const blueFrequencyOption = computed(() =>
  chartOption(blueFrequencyFacts.value, '蓝球期频率（当前窗口）', '#1c64b3', '期频率'),
)

const qualityWindows = computed(() => analysis.value?.quality_summary.windows ?? [])
const analysisFacts = computed(() => windowFacts.value.slice(0, 240))

function validWindows(): WindowSpec[] {
  return selectedWindows.value.filter((window) =>
    WINDOW_OPTIONS.includes(window),
  )
}

function maxNumericWindow(): number | null {
  const values = validWindows()
  if (values.includes('all')) return null
  const numeric = values.filter((value): value is number => typeof value === 'number')
  return numeric.length ? Math.max(...numeric) : null
}

async function ensureDraws(): Promise<boolean> {
  if (!draws.loadedAt) await draws.load()
  if (draws.error) {
    loadError.value = draws.error
    return false
  }
  if (draws.count === 0) {
    loadError.value = clientError('后端没有已取得的开奖记录，无法创建快照')
    return false
  }
  return true
}

async function createAndRun(): Promise<void> {
  loadError.value = null
  submitting.value = true
  try {
    if (!validWindows().length) {
      throw clientError('至少选择一个统计窗口')
    }
    if (!(await ensureDraws())) return

    const count = maxNumericWindow() ?? draws.count
    const range = draws.range(count)
    if (!range) throw clientError('没有可用的期号区间')

    snapshot.value = await apiPost<Snapshot>('/snapshots', {
      game: 'ssq',
      start_issue: range.start_issue,
      end_issue: range.end_issue,
    })

    const payload = await apiPost<CreateJobResponse>('/analysis-jobs', {
      snapshot_id: snapshot.value.snapshot_id,
      windows: validWindows(),
    })
    jobId.value = payload.job_id
    launch({
      id: payload.job_id,
      kind: 'analysis.run',
      label: `统计快照 #${snapshot.value.snapshot_id}`,
      status: payload.status,
    })
  } catch (caught) {
    loadError.value = toApiError(caught)
  } finally {
    submitting.value = false
  }
}

async function loadAnalysis(id: number): Promise<void> {
  resultLoading.value = true
  try {
    const payload = await apiGet<AnalysisResponse>(`/analyses/${id}`)
    analysis.value = payload.analysis
    const windows = payload.analysis.windows
    if (!windows.includes(selectedWindow.value)) selectedWindow.value = windows[0] ?? 100
  } catch (caught) {
    loadError.value = toApiError(caught)
  } finally {
    resultLoading.value = false
  }
}

async function loadFromInput(): Promise<void> {
  const id = Number(analysisInput.value.trim())
  if (!Number.isInteger(id) || id < 1) {
    loadError.value = clientError('统计结果号必须为正整数')
    return
  }
  await loadAnalysis(id)
}

watch(
  () => trackedJob.value?.updatedAt,
  () => {
    if (trackedJob.value?.status !== 'succeeded') return
    const result = trackedJob.value?.result
    if (result && typeof result === 'object' && 'analysis_run_id' in result) {
      const id = Number((result as Record<string, unknown>).analysis_run_id)
      if (Number.isInteger(id) && id > 0) void loadAnalysis(id)
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
        <h1>统计分析</h1>
        <p class="view-subtitle">30 / 100 / 300 期与全范围：频次、遗漏、和值、奇偶与分区。</p>
      </div>
      <StatusPill
        :tone="jobStatus?.tone ?? (analysis ? 'ok' : 'neutral')"
        :label="jobStatus?.label ?? (analysis ? '结果已读取' : '尚未创建统计任务')"
      />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      红球期频率之和为 6，蓝球为 1；当前遗漏从截止期向前连续未出现的实际开奖期数计算，
      窗口内从未出现记为“至少 n 期”。图表与表格只渲染后端事实集，前端不复制统计算法。
    </p>

    <SectionCard title="快照与窗口" description="先固定快照，再提交统计任务；页面轮询任务状态">
      <div class="checkbox-row">
        <label v-for="window in WINDOW_OPTIONS" :key="String(window)">
          <input v-model="selectedWindows" type="checkbox" :value="window" />
          {{ windowLabel(window) }}
        </label>
      </div>
      <p class="muted">
        快照区间默认取已取得的最新 N 期（N 为所选最大数字窗口；选择“全部”时使用全部已取得数据）。
        当前已缓存 {{ formatCount(draws.count) }}。
      </p>
      <div v-if="snapshot" class="data-status-grid">
        <DataStatus label="快照号" :value="snapshot.snapshot_id" />
        <DataStatus label="区间" :value="`${snapshot.start_issue}—${snapshot.end_issue}`" />
        <DataStatus label="成员" :value="formatCount(snapshot.member_count)" />
        <DataStatus label="快照哈希" :value="snapshot.hash.slice(0, 16)" />
      </div>
      <p v-if="trackedJob" class="muted">
        任务 #{{ trackedJob.id }}：{{ jobStatus?.label }} · {{ jobs.progressOf(trackedJob).text }}
        <template v-if="trackedJob.errorCode">
          · 错误码 <code>{{ trackedJob.errorCode }}</code>
          <template v-if="trackedJob.errorMessage">（{{ trackedJob.errorMessage }}）</template>
        </template>
      </p>
      <p v-if="resultLoading" class="loading-note">正在读取统计结果与事实集…</p>
      <ErrorNote :error="loadError" context="创建统计任务" />
      <ErrorNote :error="jobError" context="统计任务" />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" :disabled="submitting" @click="createAndRun">
            {{ submitting ? '提交中…' : '创建快照并统计' }}
          </button>
          <label class="field analysis-inline-field">
            <span class="field-label">按结果号读取</span>
            <input v-model="analysisInput" class="field-input" type="text" inputmode="numeric" placeholder="analyses id" />
          </label>
          <button type="button" class="ghost-button" @click="loadFromInput">读取结果</button>
        </div>
      </template>
    </SectionCard>

    <template v-if="analysis">
      <SectionCard
        title="质量摘要"
        description="样本数、截止期、窗口截断与不足说明"
        :tag="analysis.metric_version"
        tag-tone="info"
        wide
      >
        <div class="data-status-grid">
          <DataStatus label="分析号" :value="analysis.id" />
          <DataStatus label="有效样本" :value="formatCount(analysis.quality_summary.sample_n)" />
          <DataStatus label="截止期" :value="analysis.quality_summary.as_of_issue ?? '—'" />
          <DataStatus label="截止日期" :value="analysis.quality_summary.as_of_date ?? '—'" />
          <DataStatus label="事实数量" :value="analysis.fact_count" />
          <DataStatus label="分析哈希" :value="analysis.analysis_hash.slice(0, 16)" />
          <DataStatus
            label="快照哈希"
            :value="analysis.snapshot ? analysis.snapshot.hash.slice(0, 16) : '—'"
            :unwired="analysis.snapshot === null"
          />
        </div>
        <div class="table-shell">
          <table class="data-table">
            <caption class="muted">窗口质量：{{ analysis.quality_summary.notes.join('；') || '无额外说明' }}</caption>
            <thead>
              <tr>
                <th scope="col">窗口</th>
                <th scope="col">请求</th>
                <th scope="col">有效样本</th>
                <th scope="col">不足</th>
                <th scope="col">左截断</th>
                <th scope="col">说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in qualityWindows" :key="String(item.window)">
                <td>{{ windowLabel(item.window) }}</td>
                <td>{{ item.requested ?? '全部' }}</td>
                <td>{{ item.sample_n }}</td>
                <td>{{ item.insufficient ? '是' : '否' }}</td>
                <td>{{ item.truncated_left ? '是' : '否' }}</td>
                <td>{{ item.note }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionCard>

      <SectionCard title="图表窗口" description="切换展示窗口不会改写任务结果">
        <label class="field">
          <span class="field-label">展示窗口</span>
          <select v-model="selectedWindow" class="field-input">
            <option v-for="window in analysis.windows" :key="String(window)" :value="window">
              {{ windowLabel(window) }}
            </option>
          </select>
        </label>
        <p class="muted">
          当前窗口事实 {{ windowFacts.length }} 条；每张图标题与样本数在质量摘要中可核对。
        </p>
      </SectionCard>

      <SectionCard title="红球期频率与遗漏" description="33 个红球，按当前窗口事实绘图" wide>
        <div class="chart-grid">
          <EChart :option="redFrequencyOption" label="红球期频率柱状图" :height="280" />
          <EChart :option="redOmissionOption" label="红球当前遗漏柱状图" :height="280" />
        </div>
      </SectionCard>

      <SectionCard title="蓝球期频率与遗漏" description="16 个蓝球，同一窗口与样本" wide>
        <EChart :option="blueFrequencyOption" label="蓝球期频率柱状图" :height="240" />
        <div class="table-shell">
          <table class="data-table">
            <caption class="muted">蓝球遗漏事实</caption>
            <thead>
              <tr>
                <th scope="col">事实 ID</th>
                <th scope="col">遗漏</th>
                <th scope="col">单位</th>
                <th scope="col">下界</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="fact in blueOmissionFacts" :key="fact.fact_id">
                <td>{{ fact.fact_id }}</td>
                <td>{{ fact.value }}</td>
                <td>{{ fact.unit }}</td>
                <td>{{ fact.lower_bound ? '是' : '否' }}</td>
              </tr>
              <tr v-if="blueOmissionFacts.length === 0">
                <td class="table-empty" colspan="4">该窗口没有蓝球遗漏事实</td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionCard>

      <SectionCard title="结构指标" description="和值、跨度、奇偶、分区与连号；样本不足时不下结论" wide>
        <div class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">事实 ID</th>
                <th scope="col">数值</th>
                <th scope="col">单位</th>
                <th scope="col">样本</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="fact in structureFacts" :key="fact.fact_id">
                <td>{{ fact.fact_id }}</td>
                <td>{{ fact.unit === 'ratio' ? formatRatio(fact.value) : fact.value }}</td>
                <td>{{ fact.unit }}</td>
                <td>{{ fact.sample_n }}</td>
              </tr>
              <tr v-if="structureFacts.length === 0">
                <td class="table-empty" colspan="4">该窗口没有结构事实</td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionCard>

      <SectionCard title="事实明细" description="带 fact_id、样本数与窗口的原始事实" wide>
        <div class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">事实 ID</th>
                <th scope="col">数值</th>
                <th scope="col">单位</th>
                <th scope="col">样本</th>
                <th scope="col">窗口</th>
                <th scope="col">下界</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="fact in analysisFacts" :key="fact.fact_id">
                <td>{{ fact.fact_id }}</td>
                <td>{{ fact.unit === 'ratio' ? formatRatio(fact.value) : fact.value }}</td>
                <td>{{ fact.unit }}</td>
                <td>{{ fact.sample_n }}</td>
                <td>{{ windowLabel(fact.window) }}</td>
                <td>{{ fact.lower_bound ? '是' : '否' }}</td>
              </tr>
              <tr v-if="analysisFacts.length === 0">
                <td class="table-empty" colspan="6">当前窗口没有事实</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="muted">列表最多显示 240 条；完整事实以 /analyses/{{ analysis.id }} 为准。</p>
        <template #actions>
          <RouterLink class="action-link" :to="{ path: '/ai', query: { snapshot: analysis.snapshot_id } }">
            发送至 AI 分析
          </RouterLink>
        </template>
      </SectionCard>
    </template>

    <SectionCard v-else title="统计结果" description="任务完成后按 analysis_run_id 读取 /analyses/{id}" wide>
      <EmptyState
        title="暂无统计结果"
        description="创建快照并提交统计任务；任务成功后自动读取事实集、质量摘要与图表。"
        hint="旧任务结果不会覆盖新快照；页面只展示当前读取的结果号。"
      />
      <p class="muted">最近读取时间：{{ formatSyncTime(draws.loadedAt) }}。</p>
    </SectionCard>
  </div>
</template>
