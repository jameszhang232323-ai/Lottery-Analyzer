<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { apiPost, toApiError, type ApiError, type CreateJobResponse } from '../api/client'
import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorNote from '../components/ErrorNote.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
import { useJobLauncher } from '../composables/useJobLauncher'
import { useAuthStore } from '../stores/auth'
import { useCandidatesStore } from '../stores/candidates'
import { useDrawsStore } from '../stores/draws'
import { useJobsStore } from '../stores/jobs'
import { useSystemStore } from '../stores/system'
import {
  describeCandidateStatus,
  describeCoverage,
  describeDrawQuality,
  describeJobStatus,
  formatCount,
  formatSyncTime,
} from '../stores/selectors'

const auth = useAuthStore()
const system = useSystemStore()
const draws = useDrawsStore()
const jobs = useJobsStore()
const candidates = useCandidatesStore()
const { launch } = useJobLauncher()

const loading = ref(false)
const syncing = ref(false)
const syncError = ref<ApiError | null>(null)

const latest = computed(() => draws.latest)
const latestNumbers = computed(() => {
  const draw = latest.value
  if (!draw) return { reds: [] as number[], blue: null as number | null }
  if (draw.numbers.type === 'ssq') return { reds: draw.numbers.reds, blue: draw.numbers.blue }
  return { reds: draw.numbers.digits, blue: null }
})
const latestQuality = computed(() => describeDrawQuality(latest.value?.quality_status ?? ''))
const coverageState = computed(() => describeCoverage(system.coverage))

const loadError = computed<ApiError | null>(() => system.lastApiError ?? draws.error)

const recentSync = computed(() => {
  const items = jobs.jobs.filter((job) => job.kind === 'sync.run')
  return items.length ? items[items.length - 1] : null
})
const recentAnalysis = computed(() => {
  const items = jobs.jobs.filter((job) => job.kind === 'analysis.run')
  return items.length ? items[items.length - 1] : null
})
const recentBacktests = computed(() => jobs.jobs.filter((job) => job.kind === 'backtest.run'))
const lockedBatches = computed(() =>
  candidates.batches.filter((batch) => ['locked', 'awaiting_result', 'settled'].includes(batch.status)),
)

function runStatus(id: number) {
  const job = jobs.find(id)
  return job ? describeJobStatus(job.status) : null
}

async function load(): Promise<void> {
  loading.value = true
  await system.refreshAll()
  loading.value = false
}

async function sync(): Promise<void> {
  syncError.value = null
  if (!auth.isAdmin) return
  syncing.value = true
  try {
    const payload = await apiPost<CreateJobResponse>('/sync-jobs', {
      game: system.enabledGames[0]?.code ?? 'ssq',
      mode: 'incremental',
    })
    launch({ id: payload.job_id, kind: 'sync.run', label: '增量同步', status: payload.status })
  } catch (caught) {
    syncError.value = toApiError(caught)
  } finally {
    syncing.value = false
  }
}

onMounted(() => {
  void draws.loadLatest()
  if (!system.datasetLoadedAt) void load()
})
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>总览</h1>
        <p class="view-subtitle">最新结果、数据质量、已锁定批次与近期实验摘要。</p>
      </div>
      <StatusPill
        :tone="system.backendStatus.tone"
        :label="system.backendStatus.label"
      />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      总览只汇总后端返回的真实快照；数据未初始化时引导配置，不以任何非真实号码、成绩或收益填充页面。
      覆盖统计取已取得的最新最多 300 期，避免把尚未开奖的日历期算成缺期。
    </p>

    <p v-if="loading" class="loading-note">正在读取彩种、开奖数据与覆盖清单…</p>
    <ErrorNote :error="loadError" context="读取总览数据" />
    <ErrorNote :error="syncError" context="提交同步任务" />

    <div class="section-grid">
      <SectionCard
        title="最新结果"
        description="截至期号、开奖日期与真实号码"
        tag="真实数据"
        tag-tone="ok"
      >
        <template v-if="latest">
          <div class="data-status-grid">
            <DataStatus label="截至期号" :value="latest.issue" />
            <DataStatus label="开奖日期" :value="latest.draw_date" />
            <DataStatus label="修订号" :value="`r${latest.revision}`" />
            <DataStatus
              label="质量"
              :value="latestQuality.label"
              :tone="latestQuality.tone"
              :title="latestQuality.detail"
            />
          </div>
          <div class="ball-row" :aria-label="`${latest.issue} 期开奖号码`">
            <span v-for="red in latestNumbers.reds" :key="`red-${red}`" class="ball ball--red">
              {{ String(red).padStart(2, '0') }}
            </span>
            <span v-if="latestNumbers.blue !== null" class="ball ball--blue">
              {{ String(latestNumbers.blue).padStart(2, '0') }}
            </span>
          </div>
          <p class="muted">
            来源 {{ latest.source_mode }} · 观察时间 {{ formatSyncTime(latest.observed_at) }} ·
            号码只来自后端当前修订。
          </p>
        </template>
        <EmptyState
          v-else
          title="尚无开奖记录"
          description="初始化并同步后，这里显示最新一期与 6 红 1 蓝号码。"
          hint="没有数据时显示空状态，不自行拼装号码或补位。"
        />
        <template #actions>
          <div class="action-row">
            <button
              type="button"
              class="action-button"
              :disabled="!auth.isAdmin || syncing"
              :title="auth.isAdmin ? '创建增量同步任务' : '需要管理员角色'"
              @click="sync"
            >
              {{ syncing ? '提交中…' : '同步数据' }}
            </button>
            <RouterLink class="action-link" to="/draws">查看开奖数据</RouterLink>
            <RouterLink class="action-link" to="/analysis">进入分析</RouterLink>
            <span v-if="!auth.isAdmin" class="unwired-note">同步需要管理员角色</span>
          </div>
        </template>
      </SectionCard>

      <SectionCard
        title="数据质量"
        description="完整性、核验级别与覆盖区间"
        :tag="coverageState.label"
        :tag-tone="coverageState.tone"
      >
        <div class="data-status-grid">
          <DataStatus
            label="有效期数"
            :value="formatCount(system.dataset.validDrawCount)"
            :unwired="system.dataset.validDrawCount === null"
            :note="system.dataset.rangeLabel ? `区间 ${system.dataset.rangeLabel}` : ''"
          />
          <DataStatus
            label="预计期数"
            :value="formatCount(system.coverage?.expected_count ?? null)"
            :unwired="system.coverage === null"
          />
          <DataStatus
            label="缺失期数"
            :value="formatCount(system.coverage?.missing_count ?? null)"
            :unwired="system.coverage === null"
            :tone="(system.coverage?.missing_count ?? 0) > 0 ? 'warn' : 'neutral'"
          />
          <DataStatus
            label="冲突期数"
            :value="formatCount(system.coverage?.disputed_count ?? null)"
            :unwired="system.coverage === null"
            :tone="(system.coverage?.disputed_count ?? 0) > 0 ? 'error' : 'neutral'"
          />
        </div>
        <p class="muted">{{ coverageState.detail }}</p>
        <p class="muted">
          最近同步时间没有独立接口，保持“待确认”；完整性来自覆盖清单的真实计数。
        </p>
      </SectionCard>

      <SectionCard title="已锁定批次" description="本会话读取到的批次与核对状态">
        <template v-if="lockedBatches.length">
          <div class="data-status-grid">
            <DataStatus
              v-for="batch in lockedBatches"
              :key="batch.id"
              :label="`批次 #${batch.id}`"
              :value="describeCandidateStatus(batch.status).label"
              :tone="describeCandidateStatus(batch.status).tone"
              :note="`目标期 ${batch.target_issue}`"
            />
          </div>
        </template>
        <EmptyState
          v-else
          title="没有已锁定批次"
          description="本会话尚未读取到锁定批次；后端没有候选批次列表接口，需在候选记录页按批次号读取。"
          hint="锁定需要目标期日历与截止证据，锁定后核心内容不可修改。"
        />
        <template #actions>
          <RouterLink class="action-link" to="/candidates">前往候选记录</RouterLink>
        </template>
      </SectionCard>

      <SectionCard title="近期实验摘要" description="本会话发起的统计与回测任务">
        <div class="data-status-grid">
          <DataStatus
            label="最近统计任务"
            :value="recentAnalysis ? `#${recentAnalysis.id}` : '—'"
            :unwired="recentAnalysis === null"
            :note="recentAnalysis ? `任务 ${recentAnalysis.label}` : '本会话暂无'"
          />
          <DataStatus
            label="统计状态"
            :value="recentAnalysis ? runStatus(recentAnalysis.id)?.label ?? '—' : '—'"
            :tone="recentAnalysis ? runStatus(recentAnalysis.id)?.tone ?? 'neutral' : 'neutral'"
            :unwired="recentAnalysis === null"
          />
          <DataStatus
            label="回测任务数"
            :value="recentBacktests.length"
            :unwired="recentBacktests.length === 0"
          />
          <DataStatus
            label="同步状态"
            :value="recentSync ? runStatus(recentSync.id)?.label ?? '—' : '—'"
            :tone="recentSync ? runStatus(recentSync.id)?.tone ?? 'neutral' : 'neutral'"
            :unwired="recentSync === null"
          />
        </div>
        <EmptyState
          v-if="!recentAnalysis && recentBacktests.length === 0"
          title="暂无实验摘要"
          description="统计与回测完成后，任务结果会在此汇总；后端没有实验列表接口，只显示本会话任务。"
          hint="尚未回测的策略只显示“待验证”，不显示为已验证优势。"
        />
        <template #actions>
          <RouterLink class="action-link" to="/backtests">查看回测</RouterLink>
          <RouterLink class="action-link" to="/strategies">策略实验</RouterLink>
        </template>
      </SectionCard>

      <SectionCard
        title="首次使用"
        description="从配置到首个研究闭环"
        tag="引导"
        tag-tone="info"
        wide
      >
        <ol class="step-list">
          <li>在设置页确认后端连通性；管理员可管理用户与备份。</li>
          <li>在开奖数据页创建初始化/增量同步任务，并在任务中心查看进度。</li>
          <li>在统计分析页创建快照与统计任务，查看事实集与图表。</li>
          <li>在策略实验生成候选，在候选记录锁定并核对。</li>
          <li>在历史回测比较策略与 B0 / B1，在 AI 分析查看受限建议。</li>
        </ol>
        <p class="muted">
          当前版本八个业务页全部调用真实接口；后端仍未提供的任务清单、候选列表与 AI 配置状态接口，
          页面会明确显示“后端未提供”，不用本地数据填充。
        </p>
        <template #actions>
          <div class="action-row">
            <RouterLink class="action-link" to="/settings">打开设置与任务</RouterLink>
            <RouterLink class="action-link" to="/draws">初始化开奖数据</RouterLink>
          </div>
        </template>
      </SectionCard>
    </div>
  </div>
</template>
