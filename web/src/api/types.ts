/**
 * 后端 HTTP 契约类型（设计文档 11.2）。
 *
 * 首版只接通 `/health/live`、`/health/ready` 与 `/games`，其余接口在后续任务中
 * 以 OpenAPI 生成的 TypeScript 类型替换这里的手写声明。这里只描述公开数据，
 * 不含任何凭证、令牌或本地存储结构。
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

/** `/jobs/{id}` 的任务状态（后续任务接入，当前仅用于类型占位）。 */
export type JobStatus =
  | 'queued'
  | 'running'
  | 'paused'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
