/**
 * 纯状态派生（设计文档 4.5「一致的状态语义」）。
 *
 * 这一层不依赖 Vue、Pinia、DOM 或网络，只把后端返回的事实映射成
 * 「文字 + 色调」的状态描述，便于单元测试，也避免每个页面各写一套判断。
 *
 * 状态必须文字与颜色双重表达：`label` 是给用户看的文字，`tone` 只负责颜色，
 * `detail` 用于补充口径或可执行动作，界面不能只靠颜色区分状态。
 */

import type { CoverageResponse, GameInfo, JobStatus, WindowSpec } from '../api/types'

/** 状态色调；与 styles.css 中的状态 token 一一对应。 */
export type StatusTone = 'neutral' | 'ok' | 'warn' | 'error' | 'info'

/** 通用状态描述：state 为程序判断用的枚举，label 为界面文字。 */
export interface StatusDescriptor<S extends string = string> {
  state: S
  label: string
  tone: StatusTone
  detail: string
}

/* ------------------------------------------------------------------ */
/* 后端连接                                                            */
/* ------------------------------------------------------------------ */

export type BackendConnectionState = 'idle' | 'checking' | 'connected' | 'disconnected'

export interface BackendStatusInput {
  state: BackendConnectionState
  gameCount?: number
  lastError?: string
}

export type BackendStatus = StatusDescriptor<BackendConnectionState>

/** 后端连接状态：只反映健康探针与彩种清单是否可读。 */
export function describeBackend(input: BackendStatusInput): BackendStatus {
  switch (input.state) {
    case 'checking':
      return {
        state: 'checking',
        label: '检测中',
        tone: 'info',
        detail: '正在请求 /health/live、/health/ready 与 /games。',
      }
    case 'connected': {
      const count = input.gameCount ?? 0
      return {
        state: 'connected',
        label: '已连接',
        tone: 'ok',
        detail: `健康探针通过，已读取 ${count} 个彩种。`,
      }
    }
    case 'disconnected':
      return {
        state: 'disconnected',
        label: '未连接',
        tone: 'error',
        detail: input.lastError ? `后端不可用：${input.lastError}` : '无法连接后端服务。',
      }
    default:
      return {
        state: 'idle',
        label: '未检测',
        tone: 'neutral',
        detail: '进入页面后会自动检测后端连接。',
      }
  }
}

/* ------------------------------------------------------------------ */
/* 数据集状态                                                          */
/* ------------------------------------------------------------------ */

export type ReadinessState = 'unknown' | 'uninitialized' | 'unverified' | 'stale' | 'current'

export interface DatasetState {
  /** 开奖数据接口是否已接通；首版为 false，所有数据字段显示“未接入”。 */
  apiAvailable: boolean
  validDrawCount: number | null
  latestIssue: string | null
  expectedLatestIssue: string | null
  lastSyncedAt: string | null
  expectedDrawCount: number | null
  missingDrawCount: number | null
  conflictCount: number | null
  integrity: IntegrityState
  verification: VerificationState
  /** 覆盖统计实际使用的期号区间；仅用于向用户说明有效期数的口径。 */
  rangeLabel?: string | null
}

/** 首版占位数据集：接口未接通，字段保持 null，不做任何填充。 */
export const EMPTY_DATASET: DatasetState = {
  apiAvailable: false,
  validDrawCount: null,
  latestIssue: null,
  expectedLatestIssue: null,
  lastSyncedAt: null,
  expectedDrawCount: null,
  missingDrawCount: null,
  conflictCount: null,
  integrity: 'unknown',
  verification: 'unknown',
  rangeLabel: null,
}

export type ReadinessStatus = StatusDescriptor<ReadinessState>

const DEFAULT_STALE_AFTER_HOURS = 36
const HOUR_MS = 60 * 60 * 1000

/**
 * 数据是否落后（设计 4.5）。
 *
 * - 接口未接通 → 未接入，不猜测数据新旧；
 * - 无任何记录 → 未初始化，引导配置；
 * - 已知截止期与预期期号不一致 → 数据落后；
 * - 有记录但没有同步确认、或超过时效 → 待确认 / 数据落后；
 * - 其余为已确认最新。
 */
export function describeReadiness(
  dataset: DatasetState,
  now: Date = new Date(),
  staleAfterHours: number = DEFAULT_STALE_AFTER_HOURS,
): ReadinessStatus {
  if (!dataset.apiAvailable) {
    return {
      state: 'unknown',
      label: '未接入',
      tone: 'neutral',
      detail: '开奖数据接口尚未接入，无法判断数据是否落后。',
    }
  }

  if (dataset.latestIssue === null || dataset.validDrawCount === null) {
    return {
      state: 'uninitialized',
      label: '未初始化',
      tone: 'warn',
      detail: '尚未取得任何开奖记录，请先初始化五年数据。',
    }
  }

  if (dataset.validDrawCount <= 0) {
    return {
      state: 'uninitialized',
      label: '未初始化',
      tone: 'warn',
      detail: '有效数据为空，请先初始化或补漏。',
    }
  }

  if (
    dataset.expectedLatestIssue !== null &&
    dataset.expectedLatestIssue !== dataset.latestIssue
  ) {
    return {
      state: 'stale',
      label: '数据落后',
      tone: 'warn',
      detail: `截至 ${dataset.latestIssue}，尚未确认最新（预期 ${dataset.expectedLatestIssue}）。`,
    }
  }

  if (dataset.lastSyncedAt === null) {
    return {
      state: 'unverified',
      label: '待确认',
      tone: 'info',
      detail: `截至 ${dataset.latestIssue}，尚无同步确认记录；历史研究可继续。`,
    }
  }

  const syncedAt = new Date(dataset.lastSyncedAt)
  if (Number.isNaN(syncedAt.getTime())) {
    return {
      state: 'unverified',
      label: '待确认',
      tone: 'info',
      detail: `截至 ${dataset.latestIssue}，同步时间无法解析。`,
    }
  }

  const hours = (now.getTime() - syncedAt.getTime()) / HOUR_MS
  if (hours > staleAfterHours) {
    return {
      state: 'stale',
      label: '数据落后',
      tone: 'warn',
      detail: `截至 ${dataset.latestIssue}，最近同步 ${formatSyncTime(dataset.lastSyncedAt)}，已超过 ${staleAfterHours} 小时未确认最新。`,
    }
  }

  return {
    state: 'current',
    label: '已确认最新',
    tone: 'ok',
    detail: `截至 ${dataset.latestIssue}，最近同步 ${formatSyncTime(dataset.lastSyncedAt)}。`,
  }
}

/* ------------------------------------------------------------------ */
/* 完整性与核验级别                                                    */
/* ------------------------------------------------------------------ */

export type IntegrityState = 'unknown' | 'incomplete' | 'conflict' | 'complete'

export type IntegrityStatus = StatusDescriptor<IntegrityState>

/**
 * 完整性：只有日历范围可信、预计期数已解释、无未解决缺期和冲突时才算完整。
 * 未知计数不能当成 0，也不能用“已取得期数”自证完整。
 */
export function describeIntegrity(dataset: DatasetState): IntegrityStatus {
  if (!dataset.apiAvailable) {
    return {
      state: 'unknown',
      label: '未接入',
      tone: 'neutral',
      detail: '开奖数据接口尚未接入，无法评估完整性。',
    }
  }

  const { expectedDrawCount, validDrawCount, missingDrawCount, conflictCount } = dataset

  if (
    expectedDrawCount === null ||
    validDrawCount === null ||
    missingDrawCount === null ||
    conflictCount === null
  ) {
    return {
      state: 'unknown',
      label: '未评估',
      tone: 'neutral',
      detail: '完整性需要官方日历、期号清单与实际取得期数三者的组合。',
    }
  }

  if (conflictCount > 0) {
    return {
      state: 'conflict',
      label: '存在冲突',
      tone: 'error',
      detail: `${conflictCount} 期存在来源冲突，正式结算已暂停，需先复核。`,
    }
  }

  if (missingDrawCount > 0 || validDrawCount < expectedDrawCount) {
    return {
      state: 'incomplete',
      label: '不完整',
      tone: 'warn',
      detail:
        missingDrawCount > 0
          ? `预计 ${expectedDrawCount} 期，已取得 ${validDrawCount} 期，缺失 ${missingDrawCount} 期；可按实际范围分析。`
          : `预计 ${expectedDrawCount} 期，已取得 ${validDrawCount} 期，仍有期次未解释；可按实际范围分析。`,
    }
  }

  return {
    state: 'complete',
    label: '完整',
    tone: 'ok',
    detail: '预计期数已解释，且无未解决缺期和冲突。',
  }
}

export type VerificationState = 'unknown' | 'structure_only' | 'cross_source' | 'official'

export type VerificationStatus = StatusDescriptor<VerificationState>

/** 核验级别：结构通过、跨源一致、官方核对是相互独立的证据。 */
export function describeVerification(dataset: DatasetState): VerificationStatus {
  if (!dataset.apiAvailable) {
    return {
      state: 'unknown',
      label: '未接入',
      tone: 'neutral',
      detail: '开奖数据接口尚未接入，尚无核验证据。',
    }
  }

  switch (dataset.verification) {
    case 'structure_only':
      return {
        state: 'structure_only',
        label: '单源结构通过',
        tone: 'info',
        detail: '结构校验通过，尚未跨源核对；可用于探索，不进入首版正式比较集合。',
      }
    case 'cross_source':
      return {
        state: 'cross_source',
        label: '跨源一致',
        tone: 'info',
        detail: '多来源记录一致，尚未官方逐期核对；报告需披露核对比例。',
      }
    case 'official':
      return {
        state: 'official',
        label: '官方已核对',
        tone: 'ok',
        detail: '已附官方证据，可进入正式比较；仍保留修订能力。',
      }
    default:
      return {
        state: 'unknown',
        label: '未核验',
        tone: 'neutral',
        detail: '尚无核验证据；结构通过、跨源一致与官方核对分别记录。',
      }
  }
}

/* ------------------------------------------------------------------ */
/* AI 可用性                                                           */
/* ------------------------------------------------------------------ */

export type AiAvailability = 'unknown' | 'config_unknown' | 'unconfigured' | 'unavailable' | 'ready'

export interface AiStatusInput {
  apiAvailable: boolean
  configured: boolean
  /** 后端未提供配置状态查询接口时为 false；此时不能宣称“未配置”。 */
  configurationKnown?: boolean
  lastError?: string
}

export type AiStatus = StatusDescriptor<AiAvailability>

/** AI 状态。AI 不可用时，统计、草稿与已生成候选仍然保留可用。 */
export function describeAi(input: AiStatusInput): AiStatus {
  if (!input.apiAvailable) {
    return {
      state: 'unknown',
      label: '未接入',
      tone: 'neutral',
      detail: 'AI 接口尚未接入，AI 解释区不展示任何生成内容。',
    }
  }

  if (input.configurationKnown === false) {
    return {
      state: 'config_unknown',
      label: '配置状态未知',
      tone: 'neutral',
      detail: '后端未提供 AI 配置状态查询接口；提交任务后按真实结果或错误码判断。',
    }
  }

  if (!input.configured) {
    return {
      state: 'unconfigured',
      label: '未配置',
      tone: 'neutral',
      detail: '管理员配置凭证后可用；统计、草稿与回测不受影响。',
    }
  }

  if (input.lastError) {
    return {
      state: 'unavailable',
      label: 'AI 不可用',
      tone: 'error',
      detail: `AI 分析失败，可重试；统计、草稿与已生成候选保留。${input.lastError}`,
    }
  }

  return {
    state: 'ready',
    label: '可用',
    tone: 'ok',
    detail: 'AI 服务已配置；发送前仍会预览资料范围。',
  }
}

/* ------------------------------------------------------------------ */
/* 任务状态与进度                                                      */
/* ------------------------------------------------------------------ */

export type JobUiState = JobStatus | 'unknown'

export type JobStatusDescriptor = StatusDescriptor<JobUiState>

/** 任务状态的统一文字与色调；终态由 {@link isTerminalJobStatus} 判定。 */
export function describeJobStatus(status: string): JobStatusDescriptor {
  switch (status) {
    case 'queued':
      return { state: 'queued', label: '排队中', tone: 'info', detail: '任务已入队，等待 worker 领取。' }
    case 'running':
      return { state: 'running', label: '运行中', tone: 'info', detail: '任务正在由后端 worker 执行，关闭页面不会取消。' }
    case 'paused':
      return { state: 'paused', label: '已暂停', tone: 'warn', detail: '任务暂时停住，仍会轮询并等待恢复。' }
    case 'succeeded':
      return { state: 'succeeded', label: '已完成', tone: 'ok', detail: '任务成功结束，结果以后端保存的数据为准。' }
    case 'failed':
      return { state: 'failed', label: '失败', tone: 'error', detail: '任务失败，保留后端错误码与信息。' }
    case 'cancelled':
      return { state: 'cancelled', label: '已取消', tone: 'neutral', detail: '用户取消了任务；worker 确认后进入此状态。' }
    case 'interrupted':
      return { state: 'interrupted', label: '已中断', tone: 'warn', detail: '任务执行中断，可由后端续跑或重新提交。' }
    default:
      return { state: 'unknown', label: '未知状态', tone: 'neutral', detail: `后端返回了未识别的任务状态：${status || '空'}` }
  }
}

const TERMINAL_JOB_STATUSES: ReadonlySet<string> = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'interrupted',
])

/** 终态任务停止轮询；paused 仍轮询，因为后端可能恢复执行。 */
export function isTerminalJobStatus(status: string): boolean {
  return TERMINAL_JOB_STATUSES.has(status)
}

export function jobKindLabel(kind: string): string {
  switch (kind) {
    case 'sync.run':
      return '数据同步'
    case 'analysis.run':
      return '统计分析'
    case 'candidate.generate':
      return '候选生成'
    case 'backtest.run':
      return '历史回测'
    case 'ai.analyze':
      return 'AI 分析'
    default:
      return kind || '任务'
  }
}

export interface JobProgressDescriptor {
  text: string
  percent: number | null
  phase: string
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function firstFiniteNumber(source: Record<string, unknown> | null, keys: readonly string[]): number | null {
  if (!source) return null
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

function firstText(source: Record<string, unknown> | null, keys: readonly string[]): string {
  if (!source) return ''
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

/**
 * 把后端 progress / checkpoint 收敛为进度文字与百分比。
 *
 * 后端不同任务使用不同字段（processed_periods、total_periods 等），这里只读取
 * 明确存在的数值；未知时返回“—”，不推测完成比例。
 */
export function describeJobProgress(progress: unknown, checkpoint: unknown = null): JobProgressDescriptor {
  const source = asRecord(progress) ?? asRecord(checkpoint)
  if (source === null) {
    if (typeof progress === 'string' && progress.trim()) {
      return { text: progress.trim(), percent: null, phase: '' }
    }
    return { text: '—', percent: null, phase: '' }
  }

  const phase = firstText(source, ['phase', 'stage', 'status'])
  const completed = firstFiniteNumber(source, [
    'processed_periods',
    'completed',
    'completed_count',
    'done',
    'current',
  ])
  const total = firstFiniteNumber(source, [
    'total_periods',
    'total',
    'total_count',
    'periods',
    'planned',
  ])
  const message = firstText(source, ['message', 'detail', 'note'])

  if (completed !== null && total !== null && total > 0) {
    const percent = Math.max(0, Math.min(100, Math.round((completed / total) * 100)))
    return {
      text: `${phase ? `${phase} ` : ''}${completed}/${total}（${percent}%）`,
      percent,
      phase,
    }
  }
  if (completed !== null) {
    return { text: `${phase ? `${phase} ` : ''}已完成 ${completed}`, percent: null, phase }
  }
  if (message) return { text: message, percent: null, phase }
  if (phase) return { text: phase, percent: null, phase }
  return { text: '—', percent: null, phase }
}

/* ------------------------------------------------------------------ */
/* 开奖质量、覆盖与候选状态                                            */
/* ------------------------------------------------------------------ */

export type DrawQualityState = 'unknown' | 'unverified' | 'verified' | 'disputed'

/** 单期质量：文字 + 色调，避免只靠红绿区分。 */
export function describeDrawQuality(status: string): StatusDescriptor<DrawQualityState> {
  switch (status) {
    case 'verified':
      return { state: 'verified', label: '已核验', tone: 'ok', detail: '该修订有已核验来源证据。' }
    case 'unverified':
      return { state: 'unverified', label: '未核验', tone: 'info', detail: '单源结构通过或尚未取得核验证据。' }
    case 'disputed':
      return { state: 'disputed', label: '存在冲突', tone: 'error', detail: '来源之间矛盾，正式结算暂停，需先复核。' }
    default:
      return { state: 'unknown', label: '未评估', tone: 'neutral', detail: '后端未返回该修订的质量状态。' }
  }
}

export type CoverageState = 'unknown' | 'complete' | 'incomplete' | 'conflict' | 'calendar_unknown'

/** 覆盖清单状态：只有日历已知且期数已解释时才宣称完整。 */
export function describeCoverage(coverage: CoverageResponse | null): StatusDescriptor<CoverageState> {
  if (coverage === null) {
    return { state: 'unknown', label: '未查询', tone: 'neutral', detail: '尚未请求覆盖清单。' }
  }
  if (coverage.disputed_count > 0) {
    return {
      state: 'conflict',
      label: '存在冲突',
      tone: 'error',
      detail: `${coverage.disputed_count} 期存在来源冲突，正式结算暂停。`,
    }
  }
  if (!coverage.calendar_known) {
    return {
      state: 'calendar_unknown',
      label: '日历未知',
      tone: 'warn',
      detail: '缺少官方日历，无法判断该范围的预计期数与缺期。',
    }
  }
  if (coverage.complete) {
    return {
      state: 'complete',
      label: '范围完整',
      tone: 'ok',
      detail: `预计 ${coverage.expected_count ?? '—'} 期，已取得 ${coverage.acquired_count} 期，无未解决缺期与冲突。`,
    }
  }
  return {
    state: 'incomplete',
    label: '范围不完整',
    tone: 'warn',
    detail: `预计 ${coverage.expected_count ?? '—'} 期，已取得 ${coverage.acquired_count} 期，缺失 ${coverage.missing_count ?? '—'} 期。`,
  }
}

export type CandidateUiState =
  | 'unknown'
  | 'draft'
  | 'locked'
  | 'awaiting_result'
  | 'settled'
  | 'archived'

/** 候选批次生命周期；锁定后只可追加说明或归档。 */
export function describeCandidateStatus(status: string): StatusDescriptor<CandidateUiState> {
  switch (status) {
    case 'draft':
      return { state: 'draft', label: '草稿', tone: 'neutral', detail: '尚未锁定，可继续生成说明。' }
    case 'locked':
      return { state: 'locked', label: '已锁定', tone: 'info', detail: '核心内容已冻结，只能追加说明或归档。' }
    case 'awaiting_result':
      return { state: 'awaiting_result', label: '待开奖', tone: 'info', detail: '目标期尚未开奖或尚未核对。' }
    case 'settled':
      return { state: 'settled', label: '已核对', tone: 'ok', detail: '已生成结算修订；开奖更正会追加新修订。' }
    case 'archived':
      return { state: 'archived', label: '已归档', tone: 'neutral', detail: '批次已归档，历史记录保留。' }
    default:
      return { state: 'unknown', label: '未知状态', tone: 'neutral', detail: `后端返回了未识别的批次状态：${status || '空'}` }
  }
}

/* ------------------------------------------------------------------ */
/* 角色与格式化                                                        */
/* ------------------------------------------------------------------ */

export function roleLabel(role: string): string {
  switch (role) {
    case 'admin':
      return '管理员'
    case 'analyst':
      return '分析员'
    case 'viewer':
      return '访客'
    default:
      return role || '未知角色'
  }
}

/** 窗口标签：数字窗口写作“N 期”，全范围写作“全部”。 */
export function windowLabel(window: WindowSpec): string {
  return window === 'all' ? '全部' : `${window} 期`
}

/** 比率展示；保留 4 位小数仍以字符串呈现，避免前端重算统计口径。 */
export function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toFixed(4)
}

/** 安全地把未知 JSON 值展开为可读文本，用于后端返回的结构化摘要。 */
export function formatJsonValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

/* ------------------------------------------------------------------ */
/* 全局状态条                                                          */
/* ------------------------------------------------------------------ */

export type GlobalStatusKey =
  | 'game'
  | 'latest_issue'
  | 'valid_draws'
  | 'integrity'
  | 'verification'
  | 'last_sync'
  | 'backend'

export interface GlobalStatusField {
  key: GlobalStatusKey
  label: string
  value: string
  tone: StatusTone
  /** true 表示该字段尚无后端数据，界面必须同时显示“未接入”。 */
  unwired: boolean
  note: string
  detail: string
}

export interface GlobalStatusInput {
  backend: BackendStatusInput
  games: readonly GameInfo[]
  dataset: DatasetState
  now?: Date
}

function gameLabelOf(games: readonly GameInfo[]): GameInfo | null {
  return games.find((game) => game.enabled) ?? games[0] ?? null
}

/** 全局状态条七项：彩种、截至期号、有效期数、完整性、核验级别、最近同步、后端连接。 */
export function buildGlobalStatus(input: GlobalStatusInput): GlobalStatusField[] {
  const readiness = describeReadiness(input.dataset, input.now ?? new Date())
  const integrity = describeIntegrity(input.dataset)
  const verification = describeVerification(input.dataset)
  const backend = describeBackend(input.backend)
  const game = gameLabelOf(input.games)
  const dataWired = input.dataset.apiAvailable

  const fields: GlobalStatusField[] = [
    {
      key: 'game',
      label: '彩种',
      value: game ? game.name : '—',
      tone: game ? 'info' : 'neutral',
      unwired: game === null,
      note: game ? `${game.code}${game.enabled ? '' : '（预留）'}` : '彩种清单未取得',
      detail: game ? `当前彩种 ${game.name}（${game.code}）。` : '彩种清单尚未读取。',
    },
    {
      key: 'latest_issue',
      label: '截至期号',
      value: input.dataset.latestIssue ?? '—',
      tone: readiness.tone,
      unwired: !dataWired,
      note: readiness.label,
      detail: readiness.detail,
    },
    {
      key: 'valid_draws',
      label: '有效期数',
      value: formatCount(input.dataset.validDrawCount),
      tone: dataWired && (input.dataset.validDrawCount ?? 0) > 0 ? 'ok' : 'neutral',
      unwired: !dataWired || input.dataset.validDrawCount === null,
      note: input.dataset.rangeLabel ? `区间 ${input.dataset.rangeLabel}` : readiness.label,
      detail: '有效期数是可分析期数，不等同于预计期数。',
    },
    {
      key: 'integrity',
      label: '完整性',
      value: dataWired ? integrity.label : '—',
      tone: integrity.tone,
      unwired: !dataWired,
      note: '质量清单',
      detail: integrity.detail,
    },
    {
      key: 'verification',
      label: '核验级别',
      value: dataWired ? verification.label : '—',
      tone: verification.tone,
      unwired: !dataWired,
      note: '证据独立记录',
      detail: verification.detail,
    },
    {
      key: 'last_sync',
      label: '最近同步',
      value: formatSyncTime(input.dataset.lastSyncedAt),
      tone: input.dataset.lastSyncedAt ? readiness.tone : 'neutral',
      unwired: !dataWired || input.dataset.lastSyncedAt === null,
      note: 'Asia/Shanghai',
      detail: '最近一次成功同步时间；浏览器关闭不影响后端任务。',
    },
    {
      key: 'backend',
      label: '后端连接',
      value: backend.label,
      tone: backend.tone,
      unwired: false,
      note: input.backend.state === 'connected' ? `${input.backend.gameCount ?? 0} 个彩种` : '健康探针',
      detail: backend.detail,
    },
  ]

  return fields
}

/* ------------------------------------------------------------------ */
/* 格式化（纯函数）                                                     */
/* ------------------------------------------------------------------ */

const SHANGHAI_TIME_ZONE = 'Asia/Shanghai'

const shanghaiFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: SHANGHAI_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

/** 时间按 Asia/Shanghai 展示（设计 6.1）；无效或缺失返回“—”。 */
export function formatSyncTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const parts = new Map(
    shanghaiFormatter.formatToParts(date).map((part) => [part.type, part.value]),
  )
  const year = parts.get('year') ?? ''
  const month = parts.get('month') ?? ''
  const day = parts.get('day') ?? ''
  const hour = parts.get('hour') ?? ''
  const minute = parts.get('minute') ?? ''
  if (!year || !month || !day || !hour || !minute) return '—'
  return `${year}-${month}-${day} ${hour}:${minute}`
}

/** 期数展示：缺失返回“—”，不把未知当成 0。 */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value} 期`
}
