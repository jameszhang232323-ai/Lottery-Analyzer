<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import {
  apiDownload,
  apiGet,
  apiPost,
  clientError,
  toApiError,
  type ApiError,
} from '../api/client'
import type {
  CandidateBatch,
  CandidateResponse,
  ExportResponse,
  SettlementRevision,
  SettlementsResponse,
} from '../api/types'
import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorNote from '../components/ErrorNote.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
import { useCandidatesStore } from '../stores/candidates'
import { useJobsStore } from '../stores/jobs'
import {
  describeCandidateStatus,
  formatJsonValue,
  formatSyncTime,
} from '../stores/selectors'
import { saveBlob } from '../utils/download'

const route = useRoute()
const candidates = useCandidatesStore()
const jobs = useJobsStore()

const batchInput = ref('')
const selected = ref<CandidateBatch | null>(null)
const loading = ref(false)
const error = ref<ApiError | null>(null)

const settlements = ref<SettlementRevision[]>([])
const settlementsLoading = ref(false)
const settlementsError = ref<ApiError | null>(null)

const lockConfirmed = ref(false)
const locking = ref(false)
const lockError = ref<ApiError | null>(null)

const exportFormat = ref<'csv' | 'json' | 'html'>('csv')
const exporting = ref(false)
const exportResult = ref<ExportResponse | null>(null)
const exportError = ref<ApiError | null>(null)
const downloading = ref(false)

const statusCounts = computed(() => {
  const counts: Record<string, number> = {}
  for (const batch of candidates.batches) {
    counts[batch.status] = (counts[batch.status] ?? 0) + 1
  }
  return counts
})

const batchStatus = computed(() =>
  selected.value ? describeCandidateStatus(selected.value.status) : null,
)

const currentSettlement = computed(
  () => settlements.value.find((item) => item.is_current) ?? null,
)

const canLock = computed(
  () => selected.value !== null && selected.value.status === 'draft' && lockConfirmed.value,
)

async function loadBatch(id: number): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const payload = await apiGet<CandidateResponse>(`/candidates/${id}`)
    selected.value = payload.batch
    batchInput.value = String(id)
    candidates.remember(payload.batch)
    await loadSettlements(id)
  } catch (caught) {
    error.value = toApiError(caught)
    selected.value = null
    settlements.value = []
  } finally {
    loading.value = false
  }
}

async function loadSettlements(id: number): Promise<void> {
  settlementsLoading.value = true
  settlementsError.value = null
  try {
    const payload = await apiGet<SettlementsResponse>(`/candidates/${id}/settlements`)
    settlements.value = payload.items
  } catch (caught) {
    settlementsError.value = toApiError(caught)
    settlements.value = []
  } finally {
    settlementsLoading.value = false
  }
}

async function loadFromInput(): Promise<void> {
  const id = Number(batchInput.value.trim())
  if (!Number.isInteger(id) || id < 1) {
    error.value = clientError('批次号必须为正整数')
    return
  }
  await loadBatch(id)
}

async function lockBatch(): Promise<void> {
  if (!selected.value) return
  lockError.value = null
  if (!lockConfirmed.value) {
    lockError.value = clientError('请先确认锁定后核心内容不可修改')
    return
  }
  locking.value = true
  try {
    const payload = await apiPost<CandidateResponse>(`/candidates/${selected.value.id}/lock`, {
      expected_revision: selected.value.revision,
    })
    selected.value = payload.batch
    candidates.remember(payload.batch)
    lockConfirmed.value = false
    await loadSettlements(payload.batch.id)
  } catch (caught) {
    lockError.value = toApiError(caught)
  } finally {
    locking.value = false
  }
}

async function createExport(): Promise<void> {
  if (!selected.value) return
  exportError.value = null
  exporting.value = true
  try {
    exportResult.value = await apiPost<ExportResponse>('/exports', {
      batch_id: selected.value.id,
      format: exportFormat.value,
    })
  } catch (caught) {
    exportError.value = toApiError(caught)
    exportResult.value = null
  } finally {
    exporting.value = false
  }
}

async function downloadExport(): Promise<void> {
  if (!exportResult.value) return
  exporting.value = true
  exportError.value = null
  downloading.value = true
  try {
    const artifact = exportResult.value.artifact
    const file = await apiDownload(`/artifacts/${artifact.id}/download`)
    saveBlob(file.blob, file.filename || artifact.filename)
  } catch (caught) {
    exportError.value = toApiError(caught)
  } finally {
    exporting.value = false
    downloading.value = false
  }
}

async function loadSessionBatches(): Promise<void> {
  const ids = new Set<number>(candidates.batches.map((batch) => batch.id))
  for (const job of jobs.jobs) {
    if (job.kind !== 'candidate.generate') continue
    const result = job.result
    if (!result || typeof result !== 'object' || !('batch_id' in result)) continue
    const id = Number((result as Record<string, unknown>).batch_id)
    if (Number.isInteger(id) && id > 0) ids.add(id)
  }
  for (const id of ids) {
    if (!candidates.find(id)) await loadBatch(id)
  }
}

onMounted(() => {
  const raw = route.query.batch
  const value = Array.isArray(raw) ? raw[0] : raw
  const id = typeof value === 'string' ? Number(value) : Number.NaN
  if (Number.isInteger(id) && id > 0) {
    void loadBatch(id)
    return
  }
  const first = candidates.batches[0]
  if (first) {
    void loadBatch(first.id)
    return
  }
  void loadSessionBatches()
})
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>候选记录</h1>
        <p class="view-subtitle">草稿、锁定、待开奖、已核对与修订待重算。</p>
      </div>
      <StatusPill
        :tone="batchStatus?.tone ?? 'neutral'"
        :label="batchStatus?.label ?? '未选择批次'"
      />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      锁定采用后端或数据库时间，客户端时间不参与裁定；锁定后核心内容不可修改，只能追加说明或归档。
      同一批次与同一开奖修订只结算一次，开奖更正后追加新结算，旧结算仍可查。
    </p>

    <SectionCard title="批次状态" description="当前会话可见批次的真实数量；不是全局统计">
      <div class="status-count-grid">
        <DataStatus label="草稿" :value="statusCounts.draft ?? 0" />
        <DataStatus label="锁定" :value="statusCounts.locked ?? 0" />
        <DataStatus label="待开奖" :value="statusCounts.awaiting_result ?? 0" />
        <DataStatus label="已核对" :value="statusCounts.settled ?? 0" />
        <DataStatus label="已归档" :value="statusCounts.archived ?? 0" />
      </div>
      <p class="muted">
        后端没有“列出我的候选批次”接口，这里只统计本会话通过批次号读取或候选任务产生的批次；
        刷新页面后列表为空，可按批次号重新读取本人批次。后端批次状态也没有独立的“修订待重算”，
        开奖更正后的差异由结算修订链体现。
      </p>
    </SectionCard>

    <SectionCard title="批次列表" description="目标期、候选组数、状态与锁定时间" wide>
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">按批次号读取</span>
          <div class="inline-input-row">
            <input v-model="batchInput" class="field-input" type="text" inputmode="numeric" placeholder="candidates id" />
            <button type="button" class="ghost-button" :disabled="loading" @click="loadFromInput">读取</button>
          </div>
        </label>
      </div>
      <p v-if="loading" class="loading-note">正在读取候选批次…</p>
      <ErrorNote :error="error" context="读取候选批次" />
      <div v-if="candidates.batches.length" class="table-shell">
        <table class="data-table">
          <thead>
            <tr>
              <th scope="col">批次</th>
              <th scope="col">目标期</th>
              <th scope="col">候选组数</th>
              <th scope="col">状态</th>
              <th scope="col">锁定时间</th>
              <th scope="col">结算</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="batch in candidates.batches" :key="batch.id">
              <td>#{{ batch.id }}</td>
              <td>{{ batch.target_issue }}</td>
              <td>{{ batch.candidate_count }}</td>
              <td>
                <StatusPill
                  :tone="describeCandidateStatus(batch.status).tone"
                  :label="describeCandidateStatus(batch.status).label"
                />
              </td>
              <td>{{ formatSyncTime(batch.locked_at) }}</td>
              <td>{{ batch.settlement ? '有' : '—' }}</td>
              <td>
                <button type="button" class="ghost-button" @click="loadBatch(batch.id)">查看</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <EmptyState
        v-else
        title="本次会话暂无批次"
        description="可在策略实验生成候选，或输入本人批次号读取详情。"
        hint="他人批次与不存在批次统一返回 404，界面不泄露存在性。"
      />
      <template #actions>
        <RouterLink class="action-link" to="/strategies">去生成候选</RouterLink>
      </template>
    </SectionCard>

    <template v-if="selected">
      <SectionCard
        title="批次详情"
        :description="`目标期 ${selected.target_issue}；锁定哈希与来源快照`"
        :tag="`批次 #${selected.id}`"
        :tag-tone="batchStatus?.tone ?? 'neutral'"
        wide
      >
        <div class="data-status-grid">
          <DataStatus label="状态" :value="batchStatus?.label ?? selected.status" :tone="batchStatus?.tone ?? 'neutral'" />
          <DataStatus label="当前修订" :value="selected.revision" />
          <DataStatus label="目标期" :value="selected.target_issue" />
          <DataStatus label="候选组数" :value="selected.candidate_count" />
          <DataStatus label="锁定时间" :value="formatSyncTime(selected.locked_at)" />
          <DataStatus label="锁定哈希" :value="selected.lock_hash ? selected.lock_hash.slice(0, 16) : '—'" />
          <DataStatus label="种子" :value="selected.seed_hex.slice(0, 16)" :note="selected.seed_hex" />
          <DataStatus label="RNG 版本" :value="selected.rng_version" />
          <DataStatus label="快照区间" :value="selected.snapshot ? `${selected.snapshot.start_issue}—${selected.snapshot.end_issue}` : '—'" />
          <DataStatus label="策略类型" :value="selected.strategy?.strategy_type ?? '—'" />
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
              <tr v-for="line in selected.lines" :key="line.line_no">
                <td>{{ line.line_no }}</td>
                <td>
                  <span class="ball-row ball-row--compact">
                    <span v-for="red in line.reds" :key="`${line.line_no}-${red}`" class="ball ball--red">
                      {{ String(red).padStart(2, '0') }}
                    </span>
                  </span>
                </td>
                <td><span class="ball ball--blue">{{ String(line.blue).padStart(2, '0') }}</span></td>
                <td>{{ line.line_hash.slice(0, 16) }}</td>
              </tr>
              <tr v-if="selected.lines.length === 0">
                <td class="table-empty" colspan="4">批次没有号码行</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="selected.settlement" class="muted">
          当前结算修订 #{{ selected.settlement.id }}：{{ formatJsonValue(selected.settlement.summary) }}
        </p>
      </SectionCard>

      <SectionCard
        title="锁定"
        description="要求 expected_revision 与服务端时间裁定"
        :tag="selected.status === 'draft' ? '可锁定' : batchStatus?.label ?? '不可锁定'"
        :tag-tone="selected.status === 'draft' ? 'info' : 'neutral'"
      >
        <p class="muted">
          当前批次修订 {{ selected.revision }}。锁定时提交该版本号；如果界面已过期，后端会返回冲突错误码而不是覆盖新数据。
        </p>
        <label class="checkbox-row">
          <input v-model="lockConfirmed" type="checkbox" />
          我确认锁定后核心号码、目标期、参数与种子不可修改
        </label>
        <ErrorNote :error="lockError" context="锁定批次" />
        <template #actions>
          <button
            type="button"
            class="action-button"
            :disabled="!canLock || locking"
            @click="lockBatch"
          >
            {{ locking ? '锁定中…' : '锁定批次' }}
          </button>
        </template>
      </SectionCard>

      <SectionCard title="结算与修订" description="GET /candidates/{id}/settlements；旧修订保留" wide>
        <p v-if="settlementsLoading" class="loading-note">正在读取结算修订…</p>
        <ErrorNote :error="settlementsError" context="读取结算" />
        <div v-if="settlements.length" class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">结算修订</th>
                <th scope="col">开奖修订</th>
                <th scope="col">被取代</th>
                <th scope="col">当前</th>
                <th scope="col">时间</th>
                <th scope="col">摘要</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in settlements" :key="item.id">
                <td>#{{ item.id }}</td>
                <td>{{ item.draw_revision_id }}</td>
                <td>{{ item.supersedes_id ?? '—' }}</td>
                <td>{{ item.is_current ? '是' : '否' }}</td>
                <td>{{ formatSyncTime(item.created_at) }}</td>
                <td><code class="cell-code">{{ formatJsonValue(item.summary) }}</code></td>
              </tr>
            </tbody>
          </table>
        </div>
        <EmptyState
          v-else
          title="暂无结算记录"
          description="开奖后由后端核对服务生成；来源冲突时不结算。后端当前没有通过 HTTP 触发重新核对的端点。"
          hint="开奖更正会追加新结算修订，旧结算仍可查。"
        />
        <details v-if="currentSettlement" class="raw-details">
          <summary>查看当前结算命中明细（原始结果）</summary>
          <pre>{{ JSON.stringify(currentSettlement.result, null, 2) }}</pre>
        </details>
      </SectionCard>

      <SectionCard title="导出" description="POST /exports，再用鉴权 fetch 下载附件" wide>
        <div class="filter-grid">
          <label class="field">
            <span class="field-label">导出格式</span>
            <select v-model="exportFormat" class="field-input">
              <option value="csv">CSV</option>
              <option value="json">JSON</option>
              <option value="html">HTML</option>
            </select>
          </label>
        </div>
        <p class="muted">
          下载使用同源 fetch 携带 HttpOnly Cookie，再以 Blob 交给 a[download]；令牌不会出现在 URL。
        </p>
        <div v-if="exportResult" class="data-status-grid">
          <DataStatus label="附件号" :value="exportResult.artifact.id" />
          <DataStatus label="文件名" :value="exportResult.artifact.filename" />
          <DataStatus label="大小" :value="`${exportResult.artifact.size_bytes} B`" />
          <DataStatus label="格式" :value="exportResult.format" />
          <DataStatus label="报告哈希" :value="exportResult.report_hash.slice(0, 16)" />
          <DataStatus label="快照哈希" :value="(exportResult.snapshot_hash ?? '—').slice(0, 16)" />
        </div>
        <ErrorNote :error="exportError" context="导出候选" />
        <template #actions>
          <div class="action-row">
            <button type="button" class="action-button" :disabled="exporting" @click="createExport">
              {{ exporting && !downloading ? '生成中…' : '生成导出' }}
            </button>
            <button
              type="button"
              class="action-button"
              :disabled="!exportResult || exporting"
              @click="downloadExport"
            >
              {{ downloading ? '下载中…' : '下载附件' }}
            </button>
          </div>
        </template>
      </SectionCard>
    </template>

    <SectionCard v-else title="核对详情" description="命中明细、奖级与结算修订" wide>
      <EmptyState
        title="未选择批次"
        description="按批次号读取本人批次后，这里显示号码行、锁定状态与全部结算修订。"
        hint="来源冲突不结算；规则未确认时只输出命中明细。"
      />
    </SectionCard>
  </div>
</template>
