/**
 * 后端 HTTP 契约类型（设计文档 11.2）。
 *
 * 以 `backend/src/dajidali/api/routes/*.py` 的实际返回为准；后端返回 JSON，
 * 未在路由中声明的可选字段允许缺省。类型只描述公开数据，不含任何凭证。
 */

/** 统一错误体：code / message / details / request_id。 */
export interface ApiErrorBody {
  code: string
  message: string
  details: Record<string, unknown>
  request_id: string
}

/** 存活探针与就绪探针的公共形状。 */
export interface HealthStatus {
  status: string
  checks?: Record<string, string>
}

/** 号码区域定义，例如红球 6 个、范围 1—33。 */
export interface GameZone {
  count: number
  min: number
  max: number
}

/** 彩种信息；福彩 3D 首版只保留类型与接口。 */
export interface GameInfo {
  code: string
  name: string
  enabled: boolean
  note?: string
  red?: GameZone
  blue?: GameZone
}

/** `GET /games` 的响应。 */
export interface GamesResponse {
  items: GameInfo[]
}

/* ------------------------------------------------------------------ */
/* 登录与用户                                                          */
/* ------------------------------------------------------------------ */

export type UserRole = 'admin' | 'analyst' | 'viewer'

export interface UserInfo {
  id: number
  username: string
  display_name: string
  role: UserRole | string
  is_active: boolean
  created_at: string | null
  updated_at: string | null
}

export interface LoginResponse {
  user: UserInfo
}

export interface MeResponse {
  user: UserInfo
}

export interface LogoutResponse {
  status: string
  revoked: boolean
}

/* ------------------------------------------------------------------ */
/* 任务                                                                */
/* ------------------------------------------------------------------ */

export type JobStatus =
  | 'queued'
  | 'running'
  | 'paused'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export type JobKind =
  | 'sync.run'
  | 'analysis.run'
  | 'candidate.generate'
  | 'backtest.run'
  | 'ai.analyze'
  | string

/** `/jobs/{id}` 与各任务详情接口中的任务对象；字段随任务类型不同。 */
export interface JobSummary {
  id: number
  kind: JobKind
  status: JobStatus | string
  owner_id?: number | null
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  error_code?: string | null
  error_message?: string | null
  result?: unknown
  checkpoint?: unknown
  progress?: unknown
  attempt?: number | null
}

export interface JobDetailResponse {
  job: JobSummary
}

/** 耗时 POST 返回的 202 响应；不同接口还可能带 request_id、snapshot_hash 等。 */
export interface CreateJobResponse {
  job_id: number
  status: JobStatus | string
  status_url?: string
  [key: string]: unknown
}

/* ------------------------------------------------------------------ */
/* 开奖数据                                                            */
/* ------------------------------------------------------------------ */

export type DrawQualityStatus = 'unverified' | 'verified' | 'disputed' | string
export type DrawCurrentStatus = 'active' | 'superseded' | 'unknown' | string

export interface SsqNumbers {
  type: 'ssq'
  reds: number[]
  blue: number
}

export interface Fc3dNumbers {
  type: 'fc3d'
  digits: number[]
}

export type DrawNumbers = SsqNumbers | Fc3dNumbers

export interface Draw {
  game: string
  issue: string
  revision: number
  draw_date: string
  numbers: DrawNumbers
  quality_status: DrawQualityStatus
  is_current: boolean
  current_status: DrawCurrentStatus
  rule_version: string
  source_mode: string
  observed_at: string | null
  created_at: string | null
}

export interface DrawRange {
  start_issue: string | null
  end_issue: string | null
}

export interface DrawsResponse {
  game: string
  items: Draw[]
  limit: number
  next_cursor: string | null
  range: DrawRange
}

export interface DrawDetailResponse {
  draw: Draw
  revisions: Draw[]
  current_status: string
}

/** 来源原始观察的元数据；不回传原始载荷。 */
export interface ObservationItem {
  id: number
  source: string
  source_url: string | null
  retrieved_at: string | null
  payload_hash: string
  has_payload: boolean
  raw_ref: string | null
}

export interface ObservationsResponse {
  game: string
  issue: string
  items: ObservationItem[]
  count: number
}

export interface CalendarItem {
  game: string
  issue: string
  draw_date: string | null
  lock_deadline: string | null
  status: string
  evidence: string | null
}

export interface CoverageResponse {
  game: string
  start_issue: string
  end_issue: string
  status: string
  complete: boolean
  calendar_known: boolean
  calendar_covers_range: boolean
  expected_count: number | null
  acquired_count: number
  analyzable_count: number
  official_verified_count: number
  missing_count: number | null
  disputed_count: number
  closed_count: number
  delayed_count: number
  cancelled_count: number
  unexpected_count: number
  missing_issues: string[]
  disputed_issues: string[]
  closed_issues: string[]
  delayed_issues: string[]
  cancelled_issues: string[]
  unexpected_issues: string[]
  calendar_basis: string[]
  reasons: string[]
}

export interface CalendarResponse {
  game: string
  year: number | null
  known: boolean
  items: CalendarItem[]
  closed: CalendarItem[]
  basis: string[]
  status_counts: Record<string, number>
  note: string | null
}

/* ------------------------------------------------------------------ */
/* 快照与统计                                                          */
/* ------------------------------------------------------------------ */

export interface Snapshot {
  snapshot_id: number
  game: string
  start_issue: string
  end_issue: string
  hash: string
  member_count: number
  created_at: string | null
}

export type WindowSpec = number | 'all'

export type FactUnit = 'draws' | 'ratio' | 'sum' | 'span' | 'balls' | 'pairs' | string

export interface Fact {
  fact_id: string
  value: number
  unit: FactUnit
  sample_n: number
  window: WindowSpec
  metric_version: string
  lower_bound: boolean
}

export interface WindowQuality {
  window: WindowSpec
  requested: number | null
  sample_n: number
  insufficient: boolean
  truncated_left: boolean
  note: string
}

export interface AnalysisQuality {
  sample_n: number
  as_of_issue: string | null
  as_of_date: string | null
  windows: WindowQuality[]
  notes: string[]
}

export interface AnalysisSnapshotMeta {
  id: number
  game: string
  start_issue: string
  end_issue: string
  hash: string
  member_count: number
  schema_version?: string
}

export interface Analysis {
  id: number
  owner_id: number
  snapshot_id: number
  request_id: string
  metric_version: string
  windows: WindowSpec[]
  analysis_hash: string
  created_at: string | null
  job_id: number | null
  fact_count: number
  facts: Fact[]
  quality_summary: AnalysisQuality
  snapshot: AnalysisSnapshotMeta | null
}

export interface AnalysisResponse {
  analysis: Analysis
}

/* ------------------------------------------------------------------ */
/* 策略、候选与结算                                                    */
/* ------------------------------------------------------------------ */

export type StrategyType = 'S0' | 'S1'

export interface StrategyConditions {
  fixed_red: number[]
  excluded_red: number[]
  allowed_odd_counts: number[] | null
  sum_range: [number, number] | null
  zone_counts: [number, number, number] | null
  max_adjacent_pairs: number | null
}

export interface StrategyConfig {
  strategy_type: StrategyType
  strategy_version: string
  window?: WindowSpec | null
  alpha_periods?: number | null
  lam?: number | null
  conditions: StrategyConditions
}

export interface CandidateLine {
  line_no: number
  reds: number[]
  blue: number
  line_hash: string
}

export interface CandidateSettlementMeta {
  id: number
  draw_revision_id: number
  supersedes_id?: number | null
  is_current: boolean
  created_at: string | null
  summary: Record<string, unknown>
}

export interface CandidateBatch {
  id: number
  owner_id: number
  status: string
  revision: number
  target_issue: string
  candidate_count: number
  seed_hex: string
  rng_version: string
  created_at: string | null
  locked_at: string | null
  lock_hash: string | null
  note: string | null
  snapshot: {
    id: number
    game: string
    start_issue: string
    end_issue: string
    hash: string
    member_count: number
  } | null
  strategy: {
    id: number
    strategy_type: string
    version_hash: string
  } | null
  lines: CandidateLine[]
  settlement: CandidateSettlementMeta | null
  settlements: Array<{
    id: number
    draw_revision_id: number
    is_current: boolean
    created_at: string | null
  }>
}

export interface CandidateResponse {
  batch: CandidateBatch
}

export interface SettlementLine {
  line_no: number
  reds: number[]
  blue: number
  line_hash: string
  [key: string]: unknown
}

export interface SettlementRevision extends CandidateSettlementMeta {
  batch_id: number
  result: {
    schema?: string
    batch?: Record<string, unknown>
    draw?: Record<string, unknown>
    rule?: Record<string, unknown>
    lines?: SettlementLine[]
    summary?: Record<string, unknown>
    [key: string]: unknown
  }
  summary: Record<string, unknown>
}

export interface SettlementsResponse {
  batch_id: number
  items: SettlementRevision[]
  current: SettlementRevision | null
}

/* ------------------------------------------------------------------ */
/* AI                                                                  */
/* ------------------------------------------------------------------ */

export interface AiObservation {
  fact_ids: string[]
  interpretation: string
}

export interface AiStrategyProposal {
  strategy_type: StrategyType | string
  validation_status: string
  window?: WindowSpec | null
  alpha_periods?: number | null
  lambda?: number | null
  conditions?: Record<string, unknown> | null
  reason_fact_ids?: string[]
}

export interface AiReport {
  schema_version: string
  request_id: string
  snapshot_hash: string
  observations: AiObservation[]
  strategy_proposals: AiStrategyProposal[]
  limitations: string[]
  [key: string]: unknown
}

export interface AiValidationIssue {
  code: string
  path: string
  message: string
}

export interface AiValidation {
  ok?: boolean
  errors?: AiValidationIssue[]
  [key: string]: unknown
}

export interface AiUsage {
  calls?: number
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  duration_ms?: number
  raw_ref?: string | null
  call_log?: Array<Record<string, unknown>>
  error_code?: string
  [key: string]: unknown
}

export interface AiRun {
  id: number
  owner_id: number
  request_id: string
  snapshot_hash: string
  provider: string
  model: string
  prompt_version: string
  outcome: string
  job_id: number | null
  created_at: string | null
  report: AiReport | null
  validation: AiValidation
  usage: AiUsage
  request: Record<string, unknown> | null
}

export interface AiRunResponse {
  ai_run: AiRun
}

/* ------------------------------------------------------------------ */
/* 回测                                                                */
/* ------------------------------------------------------------------ */

export interface BacktestPlan {
  id: number
  owner_id: number
  name: string | null
  plan_hash: string
  primary_metric: string
  master_seed_hex: string
  holdout_state: string
  created_at: string | null
  snapshot: Record<string, unknown> | null
  requested_split: Record<string, unknown> | null
  split: Record<string, unknown> | null
  split_summary: Record<string, unknown> | null
  strategy: Record<string, unknown> | null
  registry: Record<string, unknown> | null
}

export interface BacktestPeriod {
  issue: string
  stage: string
  candidate_hash: string
  hits: unknown
  baseline: unknown
  excluded_reason: string | null
}

export interface Backtest {
  id: number
  plan_id: number
  job_id: number | null
  status: string
  started_at: string | null
  finished_at: string | null
  progress: Record<string, unknown> | null
  metrics: Record<string, unknown> | null
  bootstrap: Record<string, unknown> | null
  plan: BacktestPlan | null
  periods: BacktestPeriod[]
  counts: {
    periods: number
    evaluated: number
    excluded: number
  }
}

export interface BacktestResponse {
  backtest: Backtest
}

/* ------------------------------------------------------------------ */
/* 导出、附件、备份与用户管理                                          */
/* ------------------------------------------------------------------ */

export interface Artifact {
  id: number
  owner_id: number
  kind: string
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  created_at: string | null
  expires_at: string | null
}

export interface ExportResponse {
  artifact: Artifact
  download_url: string
  format: string
  report_hash: string
  snapshot_hash: string | null
  disclaimer: string | null
}

export interface AdminUsersResponse {
  items: UserInfo[]
}

export interface AdminUserResponse {
  user: UserInfo
}

export interface Backup {
  backup_id: string
  sha256: string
  size_bytes: number
  created_at: string | null
  manifest: Record<string, unknown>
}

export interface BackupResponse {
  backup: Backup
}

export interface RestorePreview {
  restore_id: string
  preview_hash: string
  archive_sha256: string
  database_sha256: string
  created_at: string
  backup_schema: string | null
  app_version: string | null
  table_row_counts: Record<string, number>
  excluded_tables: string[]
  redacted_secret_material: boolean
  secrets_cleared: boolean
  warnings: Array<Record<string, unknown>>
}

export interface RestorePreviewResponse {
  preview: RestorePreview
}

export interface SecretReference {
  id: number
  owner_id: number | null
  name: string
  key_version: number
  masked_hint: string
  created_at: string | null
  updated_at: string | null
}

export interface SecretsResponse {
  items: SecretReference[]
}

/** 来源 / AI 的配置状态；只含是否配置与掩码，不含密钥。 */
export interface ProviderStatus {
  id: string
  kind: 'ai' | 'source'
  configured: boolean
  provider?: string | null
  model?: string | null
  base_url?: string | null
  masked_key?: string | null
  name?: string | null
  license_verified?: boolean
  note?: string | null
}

export interface ProvidersResponse {
  items: ProviderStatus[]
}

/** 备份归档列表项；损坏归档 usable=false，reason 给出原因。 */
export interface BackupListItem {
  backup_id: string
  usable: boolean
  size_bytes: number
  sha256: string | null
  created_at: string | null
  backup_schema: string | null
  table_row_counts: Record<string, number>
  reason: string | null
}

export interface BackupsResponse {
  items: BackupListItem[]
  count: number
}
