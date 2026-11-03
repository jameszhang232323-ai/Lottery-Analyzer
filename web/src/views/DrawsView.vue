<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import {
  apiGet,
  apiPost,
  clientError,
  toApiError,
  withQuery,
  type ApiError,
  type CreateJobResponse,
} from '../api/client'
import type {
  CalendarResponse,
  CoverageResponse,
  Draw,
  DrawDetailResponse,
  DrawsResponse,
} from '../api/types'
import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorNote from '../components/ErrorNote.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
import { useJobLauncher } from '../composables/useJobLauncher'
import { useAuthStore } from '../stores/auth'
import { useDrawsStore } from '../stores/draws'
import { useJobsStore } from '../stores/jobs'
import {
  describeCoverage,
  describeDrawQuality,
  formatCount,
  formatSyncTime,
} from '../stores/selectors'

const auth = useAuthStore()
const drawsStore = useDrawsStore()
const jobs = useJobsStore()
const { launch } = useJobLauncher()

const GAME = 'ssq'
const LIMIT_OPTIONS = [20, 50, 100, 200]

const startIssue = ref('')
const endIssue = ref('')
const startDate = ref('')
const endDate = ref('')
const limit = ref(50)
const items = ref<Draw[]>([])
const nextCursor = ref<string | null>(null)
const cursorStack = ref<Array<string | null>>([null])
const pageLoading = ref(false)
const pageError = ref<ApiError | null>(null)

const selected = ref<Draw | null>(null)
const revisions = ref<Draw[]>([])
const detailLoading = ref(false)
const detailError = ref<ApiError | null>(null)

const calendar = ref<CalendarResponse | null>(null)
const calendarYear = ref(new Date().getFullYear())
const calendarLoading = ref(false)
const calendarError = ref<ApiError | null>(null)

const coverage = ref<CoverageResponse | null>(null)
const coverageStart = ref('')
const coverageEnd = ref('')
const coverageLoading = ref(false)
const coverageError = ref<ApiError | null>(null)

const syncMode = ref<'incremental' | 'review' | 'initialize'>('incremental')
const syncStart = ref('')
const syncEnd = ref('')
const syncHistoryBatch = ref(30)
const syncing = ref(false)
const syncError = ref<ApiError | null>(null)
const syncJobId = ref<number | null>(null)

const coverageState = computed(() => describeCoverage(coverage.value))
const currentCursor = computed(() => cursorStack.value[cursorStack.value.length - 1] ?? null)

const syncStatus = computed(() => {
  if (syncJobId.value === null) return null
  return jobs.find(syncJobId.value) ?? null
})

const calendarOpenItems = computed(() => {
  if (!calendar.value) return []
  return calendar.value.items.slice(0, 80)
})

function redBalls(draw: Draw): number[] {
  return draw.numbers.type === 'ssq' ? draw.numbers.reds : draw.numbers.digits
}

function blueBall(draw: Draw): number | null {
  return draw.numbers.type === 'ssq' ? draw.numbers.blue : null
}

function calendarStatusLabel(status: string): string {
  switch (status) {
    case 'scheduled':
      return '计划开奖'
    case 'closed':
      return '休市'
    case 'delayed':
      return '延期'
    case 'cancelled':
      return '取消'
    default:
      return status || '未知'
  }
}

function calendarStatusTone(status: string): 'ok' | 'warn' | 'error' | 'neutral' {
  if (status === 'scheduled') return 'ok'
  if (status === 'delayed') return 'warn'
  if (status === 'cancelled') return 'error'
  return 'neutral'
}

async function fetchPage(cursor: string | null, resetStack = false): Promise<void> {
  pageLoading.value = true
  pageError.value = null
  try {
    if (resetStack) cursorStack.value = [cursor]
    const path = withQuery('/draws', {
      game: GAME,
      start_issue: startIssue.value.trim() || null,
      end_issue: endIssue.value.trim() || null,
      start_date: startDate.value || null,
      end_date: endDate.value || null,
      limit: limit.value,
      cursor,
    })
    const payload = await apiGet<DrawsResponse>(path)
    items.value = payload.items
    nextCursor.value = payload.next_cursor
    if (payload.items.length === 0) {
      selected.value = null
      revisions.value = []
    }
  } catch (caught) {
    pageError.value = toApiError(caught)
  } finally {
    pageLoading.value = false
  }
}

async function search(): Promise<void> {
  await fetchPage(null, true)
}

async function nextPage(): Promise<void> {
  if (!nextCursor.value) return
  cursorStack.value = [...cursorStack.value, nextCursor.value]
  await fetchPage(nextCursor.value)
}

async function prevPage(): Promise<void> {
  if (cursorStack.value.length <= 1) return
  cursorStack.value = cursorStack.value.slice(0, -1)
  await fetchPage(currentCursor.value)
}

async function latestPage(): Promise<void> {
  if (!drawsStore.loadedAt) await drawsStore.load(GAME)
  if (drawsStore.error) {
    pageError.value = drawsStore.error
    return
  }
  const range = drawsStore.range(limit.value)
  if (!range) {
    pageError.value = clientError('后端没有已取得的开奖记录')
    return
  }
  startIssue.value = range.start_issue
  endIssue.value = range.end_issue
  await fetchPage(null, true)
}

async function openDetail(draw: Draw): Promise<void> {
  selected.value = draw
  revisions.value = []
  detailLoading.value = true
  detailError.value = null
  try {
    const payload = await apiGet<DrawDetailResponse>(`/draws/${gameOf(draw)}/${draw.issue}`)
    selected.value = payload.draw
    revisions.value = payload.revisions
  } catch (caught) {
    detailError.value = toApiError(caught)
  } finally {
    detailLoading.value = false
  }
}

function gameOf(draw: Draw): string {
  return draw.game || GAME
}

async function loadCalendar(): Promise<void> {
  calendarLoading.value = true
  calendarError.value = null
  try {
    calendar.value = await apiGet<CalendarResponse>(
      withQuery('/calendar', { game: GAME, year: calendarYear.value || null }),
    )
  } catch (caught) {
    calendarError.value = toApiError(caught)
  } finally {
    calendarLoading.value = false
  }
}

async function loadCoverage(): Promise<void> {
  coverageLoading.value = true
  coverageError.value = null
  try {
    let start = coverageStart.value.trim()
    let end = coverageEnd.value.trim()
    if (!start || !end) {
      if (!drawsStore.loadedAt) await drawsStore.load(GAME)
      const range = drawsStore.range(Math.max(limit.value, 30))
      if (!range) {
        coverageError.value = clientError('没有可查询覆盖的期号区间')
        return
      }
      start = range.start_issue
      end = range.end_issue
      coverageStart.value = start
      coverageEnd.value = end
    }
    coverage.value = await apiGet<CoverageResponse>(
      withQuery('/coverage', { game: GAME, start_issue: start, end_issue: end }),
    )
  } catch (caught) {
    coverageError.value = toApiError(caught)
  } finally {
    coverageLoading.value = false
  }
}

async function submitSync(): Promise<void> {
  syncError.value = null
  if (!auth.isAdmin) {
    syncError.value = clientError('同步需要管理员角色')
    return
  }
  if (syncMode.value === 'initialize' && !syncStart.value.trim()) {
    syncError.value = clientError('初始化同步必须填写起始期号')
    return
  }
  syncing.value = true
  try {
    const body: Record<string, unknown> = {
      game: GAME,
      mode: syncMode.value,
      history_batch: syncHistoryBatch.value,
    }
    if (syncStart.value.trim()) body.start_issue = syncStart.value.trim()
    if (syncEnd.value.trim()) body.end_issue = syncEnd.value.trim()
    const payload = await apiPost<CreateJobResponse>('/sync-jobs', body)
    syncJobId.value = payload.job_id
    launch({
      id: payload.job_id,
      kind: 'sync.run',
      label: `${GAME} ${syncMode.value} 同步`,
      status: payload.status,
    })
  } catch (caught) {
    syncError.value = toApiError(caught)
  } finally {
    syncing.value = false
  }
}

watch(
  () => syncStatus.value?.status,
  (status) => {
    if (status === 'succeeded') {
      void drawsStore.load(GAME, true)
      void loadCoverage()
      void fetchPage(currentCursor.value)
    }
  },
)

onMounted(() => {
  void latestPage()
  void loadCalendar()
  void loadCoverage()
})
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>开奖数据</h1>
        <p class="view-subtitle">期号筛选、号码表、单期修订链、开奖日历与覆盖清单。</p>
      </div>
      <StatusPill
        :tone="coverageState.tone"
        :label="coverage ? coverageState.label : '覆盖未查询'"
      />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      期号保存为字符串并按年份与序号规范化排序；红球为 6 个互异整数（1—33），蓝球 1 个（1—16）。
      缺失奖金显示“未取得”，不以 0 代替；冲突期暂停正式结算并保留原修订。
    </p>

    <SectionCard title="筛选与分页" description="固定游标翻页；不按期号加一猜测下一期">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">起始期号</span>
          <input v-model="startIssue" class="field-input" type="text" inputmode="numeric" placeholder="例如 2025001" />
        </label>
        <label class="field">
          <span class="field-label">结束期号</span>
          <input v-model="endIssue" class="field-input" type="text" inputmode="numeric" placeholder="例如 2025150" />
        </label>
        <label class="field">
          <span class="field-label">开始日期</span>
          <input v-model="startDate" class="field-input" type="date" />
        </label>
        <label class="field">
          <span class="field-label">结束日期</span>
          <input v-model="endDate" class="field-input" type="date" />
        </label>
        <label class="field">
          <span class="field-label">每页条数</span>
          <select v-model.number="limit" class="field-input">
            <option v-for="value in LIMIT_OPTIONS" :key="value" :value="value">{{ value }}</option>
          </select>
        </label>
      </div>
      <p class="muted">
        当前第 {{ cursorStack.length }} 页，共 {{ items.length }} 条；后端游标只记录上一页最后一期。
        期号区间与日期区间可同时使用（取交集）。
      </p>
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" :disabled="pageLoading" @click="search">
            {{ pageLoading ? '查询中…' : '查询' }}
          </button>
          <button type="button" class="action-button" :disabled="pageLoading" @click="prevPage">
            上一页
          </button>
          <button type="button" class="action-button" :disabled="pageLoading || !nextCursor" @click="nextPage">
            下一页
          </button>
          <button type="button" class="action-button" :disabled="pageLoading" @click="latestPage">
            最新一页
          </button>
          <button
            type="button"
            class="ghost-button"
            :disabled="pageLoading"
            @click="startIssue = ''; endIssue = ''; startDate = ''; endDate = ''; search()"
          >
            重置
          </button>
        </div>
      </template>
    </SectionCard>

    <ErrorNote :error="pageError" context="读取开奖记录" />

    <SectionCard
      title="开奖号码表"
      description="期号、日期、号码、质量与修订号"
      tag="真实数据"
      tag-tone="ok"
      wide
    >
      <div class="ball-legend">
        <span class="ball ball--red">红</span>
        <span class="ball-caption">红球 1—33，每期 6 个</span>
        <span class="ball ball--blue">蓝</span>
        <span class="ball-caption">蓝球 1—16，每期 1 个</span>
      </div>
      <p v-if="pageLoading" class="loading-note">正在读取 /draws…</p>
      <div class="table-shell">
        <table class="data-table">
          <caption class="muted">开奖记录表：{{ items.length }} 条</caption>
          <thead>
            <tr>
              <th scope="col">期号</th>
              <th scope="col">开奖日期</th>
              <th scope="col">红球</th>
              <th scope="col">蓝球</th>
              <th scope="col">质量</th>
              <th scope="col">修订</th>
              <th scope="col">来源</th>
              <th scope="col">详情</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="draw in items" :key="`${draw.game}-${draw.issue}-${draw.revision}`">
              <td>{{ draw.issue }}</td>
              <td>{{ draw.draw_date }}</td>
              <td>
                <span class="ball-row ball-row--compact">
                  <span v-for="red in redBalls(draw)" :key="red" class="ball ball--red">
                    {{ String(red).padStart(2, '0') }}
                  </span>
                </span>
              </td>
              <td>
                <span v-if="blueBall(draw) !== null" class="ball ball--blue">
                  {{ String(blueBall(draw)).padStart(2, '0') }}
                </span>
                <span v-else class="muted">—</span>
              </td>
              <td>
                <StatusPill
                  :tone="describeDrawQuality(draw.quality_status).tone"
                  :label="describeDrawQuality(draw.quality_status).label"
                />
              </td>
              <td>r{{ draw.revision }}</td>
              <td>{{ draw.source_mode }}</td>
              <td>
                <button type="button" class="ghost-button" @click="openDetail(draw)">查看</button>
              </td>
            </tr>
            <tr v-if="items.length === 0">
              <td class="table-empty" colspan="8">暂无开奖记录</td>
            </tr>
          </tbody>
        </table>
      </div>
    </SectionCard>

    <SectionCard
      title="单期详情与修订链"
      description="当前修订与旧修订都保留，来源与观察时间可核对"
      :tag="selected ? selected.issue : '未选择'"
      tag-tone="info"
      wide
    >
      <p v-if="detailLoading" class="loading-note">正在读取单期修订链…</p>
      <ErrorNote :error="detailError" context="读取单期详情" />
      <template v-if="selected">
        <div class="data-status-grid">
          <DataStatus label="期号" :value="selected.issue" />
          <DataStatus label="开奖日期" :value="selected.draw_date" />
          <DataStatus label="当前修订" :value="`r${selected.revision}`" />
          <DataStatus
            label="当前状态"
            :value="selected.current_status"
            :tone="selected.is_current ? 'ok' : 'warn'"
          />
          <DataStatus label="规则版本" :value="selected.rule_version" />
          <DataStatus label="来源" :value="selected.source_mode" />
        </div>
        <div class="ball-row">
          <span v-for="red in redBalls(selected)" :key="`sel-red-${red}`" class="ball ball--red">
            {{ String(red).padStart(2, '0') }}
          </span>
          <span v-if="blueBall(selected) !== null" class="ball ball--blue">
            {{ String(blueBall(selected)).padStart(2, '0') }}
          </span>
        </div>
        <ul class="timeline">
          <li v-for="revision in [...revisions].reverse()" :key="revision.revision">
            r{{ revision.revision }} · {{ revision.quality_status }} · 来源 {{ revision.source_mode }} ·
            观察 {{ formatSyncTime(revision.observed_at) }} · 写入 {{ formatSyncTime(revision.created_at) }}
            <span v-if="revision.is_current" class="ok-text">（当前）</span>
          </li>
        </ul>
        <p class="muted">
          后端未提供跨来源观察明细接口；本页只显示当前修订与修订链中的来源模式、质量与观察时间，
          不编造来源原文。
        </p>
      </template>
      <EmptyState
        v-else
        title="未选择开奖期"
        description="在上表点击“查看”，读取该期当前修订与完整修订链。"
        hint="旧修订不会被覆盖，报告会记录使用的修订号。"
      />
    </SectionCard>

    <SectionCard
      title="同步任务"
      description="管理员触发采集；接口只入队，进度在任务中心轮询"
      :tag="auth.isAdmin ? '管理员' : '只读'"
      :tag-tone="auth.isAdmin ? 'info' : 'neutral'"
    >
      <template v-if="auth.isAdmin">
        <div class="filter-grid">
          <label class="field">
            <span class="field-label">同步模式</span>
            <select v-model="syncMode" class="field-input">
              <option value="incremental">增量</option>
              <option value="review">复核最近窗口</option>
              <option value="initialize">初始化（必须填起始期号）</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">起始期号</span>
            <input v-model="syncStart" class="field-input" type="text" inputmode="numeric" />
          </label>
          <label class="field">
            <span class="field-label">结束期号</span>
            <input v-model="syncEnd" class="field-input" type="text" inputmode="numeric" />
          </label>
          <label class="field">
            <span class="field-label">历史批大小</span>
            <input v-model.number="syncHistoryBatch" class="field-input" type="number" min="1" max="200" />
          </label>
        </div>
        <p v-if="syncStatus" class="muted">
          已提交任务 #{{ syncStatus.id }}；当前状态 {{ syncStatus.status }}。
          <template v-if="syncStatus.errorCode">
            错误码 <code>{{ syncStatus.errorCode }}</code>
            <template v-if="syncStatus.errorMessage">（{{ syncStatus.errorMessage }}）</template>
          </template>
        </p>
        <ErrorNote :error="syncError" context="提交同步任务" />
      </template>
      <EmptyState
        v-else
        title="当前账号只读"
        description="只有管理员可以触发在线采集；你仍可读取全部公开开奖数据。"
        hint="后端对非管理员返回 403 ACCESS_DENIED。"
      />
      <template #actions>
        <button
          v-if="auth.isAdmin"
          type="button"
          class="action-button"
          :disabled="syncing"
          @click="submitSync"
        >
          {{ syncing ? '提交中…' : '创建同步任务' }}
        </button>
        <span v-else class="unwired-note">需要管理员角色</span>
      </template>
    </SectionCard>

    <SectionCard
      title="开奖日历"
      description="休市、延期与取消各自保留状态和依据"
      :tag="calendar?.known ? '日历已知' : '日历未知'"
      :tag-tone="calendar?.known ? 'ok' : 'warn'"
      wide
    >
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">年份</span>
          <input v-model.number="calendarYear" class="field-input" type="number" min="2000" max="2100" />
        </label>
      </div>
      <p v-if="calendarLoading" class="loading-note">正在读取 /calendar…</p>
      <ErrorNote :error="calendarError" context="读取开奖日历" />
      <div v-if="calendar" class="data-status-grid">
        <DataStatus label="条目" :value="calendar.items.length" />
        <DataStatus label="休市" :value="calendar.status_counts.closed ?? 0" />
        <DataStatus label="延期" :value="calendar.status_counts.delayed ?? 0" />
        <DataStatus label="取消" :value="calendar.status_counts.cancelled ?? 0" />
      </div>
      <p v-if="calendar && !calendar.known" class="muted">
        后端返回 {{ calendar.note || 'CALENDAR_UNKNOWN' }}：没有日历证据时无法判断缺期。
      </p>
      <div v-if="calendarOpenItems.length" class="table-shell">
        <table class="data-table">
          <thead>
            <tr>
              <th scope="col">期号</th>
              <th scope="col">开奖日期</th>
              <th scope="col">截止时间</th>
              <th scope="col">状态</th>
              <th scope="col">依据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in calendarOpenItems" :key="item.issue">
              <td>{{ item.issue }}</td>
              <td>{{ item.draw_date || '—' }}</td>
              <td>{{ formatSyncTime(item.lock_deadline) }}</td>
              <td>
                <StatusPill
                  :tone="calendarStatusTone(item.status)"
                  :label="calendarStatusLabel(item.status)"
                />
              </td>
              <td>{{ item.evidence || '未提供' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="calendar && calendar.items.length > calendarOpenItems.length" class="muted">
        当前只展示前 {{ calendarOpenItems.length }} 条，共 {{ calendar.items.length }} 条；完整日历以
        /calendar 响应为准。
      </p>
      <EmptyState
        v-else
        title="没有该年份的日历条目"
        description="后端返回空日历时不猜测休市或开奖日；可更换年份或先同步日历来源。"
      />
      <template #actions>
        <button type="button" class="action-button" :disabled="calendarLoading" @click="loadCalendar">
          读取该年日历
        </button>
      </template>
    </SectionCard>

    <SectionCard
      title="覆盖清单"
      description="预计、已取得、缺失、冲突与休市分别计数"
      :tag="coverageState.label"
      :tag-tone="coverageState.tone"
      wide
    >
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">起始期号</span>
          <input v-model="coverageStart" class="field-input" type="text" inputmode="numeric" />
        </label>
        <label class="field">
          <span class="field-label">结束期号</span>
          <input v-model="coverageEnd" class="field-input" type="text" inputmode="numeric" />
        </label>
      </div>
      <p v-if="coverageLoading" class="loading-note">正在读取 /coverage…</p>
      <ErrorNote :error="coverageError" context="读取覆盖清单" />
      <div v-if="coverage" class="data-status-grid">
        <DataStatus label="预计" :value="formatCount(coverage.expected_count)" />
        <DataStatus label="已取得" :value="formatCount(coverage.acquired_count)" />
        <DataStatus label="可分析" :value="formatCount(coverage.analyzable_count)" />
        <DataStatus label="官方核验" :value="formatCount(coverage.official_verified_count)" />
        <DataStatus label="缺失" :value="formatCount(coverage.missing_count)" :tone="(coverage.missing_count ?? 0) > 0 ? 'warn' : 'neutral'" />
        <DataStatus label="冲突" :value="formatCount(coverage.disputed_count)" :tone="coverage.disputed_count > 0 ? 'error' : 'neutral'" />
        <DataStatus label="休市" :value="formatCount(coverage.closed_count)" />
        <DataStatus label="日历覆盖" :value="coverage.calendar_covers_range ? '是' : '否'" />
      </div>
      <p v-if="coverage" class="muted">{{ coverageState.detail }}</p>
      <ul v-if="coverage?.missing_issues.length" class="plain-list">
        <li>缺失期号：{{ coverage.missing_issues.join('、') }}</li>
      </ul>
      <ul v-if="coverage?.disputed_issues.length" class="plain-list">
        <li>冲突期号：{{ coverage.disputed_issues.join('、') }}</li>
      </ul>
      <template #actions>
        <button type="button" class="action-button" :disabled="coverageLoading" @click="loadCoverage">
          查询覆盖
        </button>
      </template>
    </SectionCard>
  </div>
</template>
