import { describe, expect, it } from 'vitest'

import {
  buildGlobalStatus,
  describeAi,
  describeBackend,
  describeIntegrity,
  describeReadiness,
  describeVerification,
  EMPTY_DATASET,
  formatCount,
  formatSyncTime,
  type DatasetState,
} from '../src/stores/selectors'

const NOW = new Date('2026-11-02T14:00:00Z')

function dataset(overrides: Partial<DatasetState> = {}): DatasetState {
  return { ...EMPTY_DATASET, apiAvailable: true, ...overrides }
}

const CURRENT_DATASET = dataset({
  validDrawCount: 120,
  latestIssue: '2026080',
  expectedLatestIssue: '2026080',
  lastSyncedAt: '2026-11-02T12:00:00Z',
  expectedDrawCount: 120,
  missingDrawCount: 0,
  conflictCount: 0,
  integrity: 'complete',
  verification: 'official',
})

describe('formatSyncTime', () => {
  it('按 Asia/Shanghai 展示到分钟', () => {
    expect(formatSyncTime('2026-11-02T14:46:00Z')).toBe('2026-11-02 22:46')
    expect(formatSyncTime('2026-11-02T14:46:00+08:00')).toBe('2026-11-02 14:46')
  })

  it('缺失或无法解析时返回“—”，不写当前时间', () => {
    expect(formatSyncTime(null)).toBe('—')
    expect(formatSyncTime(undefined)).toBe('—')
    expect(formatSyncTime('')).toBe('—')
    expect(formatSyncTime('not-a-date')).toBe('—')
  })
})

describe('formatCount', () => {
  it('未知期数返回“—”，0 仍显示 0', () => {
    expect(formatCount(null)).toBe('—')
    expect(formatCount(undefined)).toBe('—')
    expect(formatCount(0)).toBe('0 期')
    expect(formatCount(120)).toBe('120 期')
  })
})

describe('describeBackend', () => {
  it('覆盖未检测、检测中、已连接与未连接', () => {
    expect(describeBackend({ state: 'idle' })).toMatchObject({
      state: 'idle',
      label: '未检测',
      tone: 'neutral',
    })
    expect(describeBackend({ state: 'checking' })).toMatchObject({
      state: 'checking',
      label: '检测中',
      tone: 'info',
    })
    expect(describeBackend({ state: 'connected', gameCount: 2 })).toMatchObject({
      state: 'connected',
      label: '已连接',
      tone: 'ok',
    })
    expect(describeBackend({ state: 'connected', gameCount: 2 }).detail).toContain('2 个彩种')
  })

  it('未连接时保留真实错误文字', () => {
    const status = describeBackend({ state: 'disconnected', lastError: '无法连接后端服务' })
    expect(status).toMatchObject({ state: 'disconnected', label: '未连接', tone: 'error' })
    expect(status.detail).toContain('无法连接后端服务')
  })
})

describe('describeReadiness', () => {
  it('接口未接通报“未接入”，不猜测数据新旧', () => {
    const status = describeReadiness(EMPTY_DATASET, NOW)
    expect(status).toMatchObject({ state: 'unknown', label: '未接入', tone: 'neutral' })
  })

  it('无任何记录时报“未初始化”并引导配置', () => {
    const status = describeReadiness(dataset(), NOW)
    expect(status).toMatchObject({ state: 'uninitialized', label: '未初始化', tone: 'warn' })
    expect(status.detail).toContain('初始化五年数据')
  })

  it('有效期为 0 也视为未初始化', () => {
    const status = describeReadiness(dataset({ validDrawCount: 0, latestIssue: '2026080' }), NOW)
    expect(status.state).toBe('uninitialized')
  })

  it('已取得期号落后于预期期号时报“数据落后”', () => {
    const status = describeReadiness(
      dataset({ latestIssue: '2026079', expectedLatestIssue: '2026080', validDrawCount: 120 }),
      NOW,
    )
    expect(status).toMatchObject({ state: 'stale', label: '数据落后', tone: 'warn' })
    expect(status.detail).toContain('2026079')
    expect(status.detail).toContain('2026080')
  })

  it('有数据但没有同步确认时报“待确认”', () => {
    const status = describeReadiness(
      dataset({ latestIssue: '2026080', validDrawCount: 120, lastSyncedAt: null }),
      NOW,
    )
    expect(status).toMatchObject({ state: 'unverified', label: '待确认', tone: 'info' })
  })

  it('超过时效未同步时报“数据落后”', () => {
    const status = describeReadiness(
      dataset({
        latestIssue: '2026080',
        validDrawCount: 120,
        lastSyncedAt: '2026-10-30T12:00:00Z',
      }),
      NOW,
    )
    expect(status.state).toBe('stale')
    expect(status.detail).toContain('36 小时')
  })

  it('同步在时效内时报“已确认最新”', () => {
    const status = describeReadiness(CURRENT_DATASET, NOW)
    expect(status).toMatchObject({ state: 'current', label: '已确认最新', tone: 'ok' })
    expect(status.detail).toContain('2026080')
  })

  it('同步时间无法解析时报“待确认”', () => {
    const status = describeReadiness(
      dataset({ latestIssue: '2026080', validDrawCount: 120, lastSyncedAt: 'bad-time' }),
      NOW,
    )
    expect(status.state).toBe('unverified')
  })
})

describe('describeIntegrity', () => {
  it('接口未接入时不评估完整性', () => {
    expect(describeIntegrity(EMPTY_DATASET)).toMatchObject({
      state: 'unknown',
      label: '未接入',
    })
  })

  it('计数未知时不把未知当 0，也不宣称完整', () => {
    expect(describeIntegrity(dataset())).toMatchObject({
      state: 'unknown',
      label: '未评估',
      tone: 'neutral',
    })
  })

  it('存在冲突时优先级最高，并暂停正式结算', () => {
    const status = describeIntegrity(
      dataset({
        expectedDrawCount: 120,
        validDrawCount: 120,
        missingDrawCount: 0,
        conflictCount: 2,
      }),
    )
    expect(status).toMatchObject({ state: 'conflict', label: '存在冲突', tone: 'error' })
    expect(status.detail).toContain('正式结算已暂停')
  })

  it('有缺失期时显示不完整', () => {
    const status = describeIntegrity(
      dataset({
        expectedDrawCount: 120,
        validDrawCount: 117,
        missingDrawCount: 3,
        conflictCount: 0,
      }),
    )
    expect(status).toMatchObject({ state: 'incomplete', label: '不完整', tone: 'warn' })
  })

  it('已取得少于预计也显示不完整', () => {
    const status = describeIntegrity(
      dataset({
        expectedDrawCount: 120,
        validDrawCount: 119,
        missingDrawCount: 0,
        conflictCount: 0,
      }),
    )
    expect(status.state).toBe('incomplete')
  })

  it('预计期数已解释且无缺期冲突时显示完整', () => {
    expect(describeIntegrity(CURRENT_DATASET)).toMatchObject({
      state: 'complete',
      label: '完整',
      tone: 'ok',
    })
  })
})

describe('describeVerification', () => {
  it('区分结构通过、跨源一致与官方核对，且未接入时不伪装证据', () => {
    expect(describeVerification(EMPTY_DATASET).label).toBe('未接入')
    expect(describeVerification(dataset()).label).toBe('未核验')
    expect(describeVerification(dataset({ verification: 'structure_only' }))).toMatchObject({
      label: '单源结构通过',
      tone: 'info',
    })
    expect(describeVerification(dataset({ verification: 'cross_source' }))).toMatchObject({
      label: '跨源一致',
      tone: 'info',
    })
    expect(describeVerification(dataset({ verification: 'official' }))).toMatchObject({
      label: '官方已核对',
      tone: 'ok',
    })
  })
})

describe('describeAi', () => {
  it('未接通的 AI 显示“未接入”', () => {
    expect(describeAi({ apiAvailable: false, configured: false })).toMatchObject({
      label: '未接入',
      tone: 'neutral',
    })
  })

  it('已接通但未配置时显示“未配置”，不影响统计', () => {
    const status = describeAi({ apiAvailable: true, configured: false })
    expect(status).toMatchObject({ label: '未配置', tone: 'neutral' })
    expect(status.detail).toContain('统计')
  })

  it('AI 失败显示“AI 不可用”，并说明统计与候选保留', () => {
    const status = describeAi({ apiAvailable: true, configured: true, lastError: '超时' })
    expect(status).toMatchObject({ label: 'AI 不可用', tone: 'error' })
    expect(status.detail).toContain('统计')
  })

  it('配置正常时显示“可用”', () => {
    expect(describeAi({ apiAvailable: true, configured: true })).toMatchObject({
      label: '可用',
      tone: 'ok',
    })
  })

  it('后端没有配置状态接口时不宣称“未配置”', () => {
    const status = describeAi({ apiAvailable: true, configured: false, configurationKnown: false })
    expect(status).toMatchObject({ label: '配置状态未知', tone: 'neutral' })
    expect(status.detail).toContain('未提供')
  })
})

describe('buildGlobalStatus', () => {
  it('固定七项，未接入字段显示“—”与未接入标记', () => {
    const fields = buildGlobalStatus({
      backend: { state: 'idle' },
      games: [],
      dataset: EMPTY_DATASET,
      now: NOW,
    })

    expect(fields.map((field) => field.label)).toEqual([
      '彩种',
      '截至期号',
      '有效期数',
      '完整性',
      '核验级别',
      '最近同步',
      '后端连接',
    ])
    expect(fields).toHaveLength(7)

    for (const key of ['latest_issue', 'valid_draws', 'integrity', 'verification', 'last_sync']) {
      const field = fields.find((item) => item.key === key)
      expect(field?.value, key).toBe('—')
      expect(field?.unwired, key).toBe(true)
    }

    const game = fields.find((field) => field.key === 'game')
    expect(game?.value).toBe('—')
    expect(game?.unwired).toBe(true)

    const backend = fields.find((field) => field.key === 'backend')
    expect(backend?.value).toBe('未检测')
    expect(backend?.unwired).toBe(false)
  })

  it('已接入且有真实数据时展示彩种、期号、有效期数与核验级别', () => {
    const fields = buildGlobalStatus({
      backend: { state: 'connected', gameCount: 2 },
      games: [{ code: 'ssq', name: '双色球', enabled: true }],
      dataset: CURRENT_DATASET,
      now: NOW,
    })

    const byKey = new Map(fields.map((field) => [field.key, field]))
    expect(byKey.get('game')).toMatchObject({
      value: '双色球',
      note: 'ssq',
      unwired: false,
    })
    expect(byKey.get('latest_issue')).toMatchObject({
      value: '2026080',
      note: '已确认最新',
      unwired: false,
    })
    expect(byKey.get('valid_draws')).toMatchObject({ value: '120 期', unwired: false })
    expect(byKey.get('integrity')).toMatchObject({ value: '完整', tone: 'ok' })
    expect(byKey.get('verification')).toMatchObject({ value: '官方已核对', tone: 'ok' })
    expect(byKey.get('last_sync')).toMatchObject({ value: '2026-11-02 20:00' })
    expect(byKey.get('backend')).toMatchObject({ value: '已连接', tone: 'ok' })
  })

  it('接口已接入但尚未初始化时，截至期号显示“—”、注明未初始化而非未接入', () => {
    const fields = buildGlobalStatus({
      backend: { state: 'connected', gameCount: 1 },
      games: [{ code: 'ssq', name: '双色球', enabled: true }],
      dataset: dataset(),
      now: NOW,
    })

    const latest = fields.find((field) => field.key === 'latest_issue')
    expect(latest).toMatchObject({ value: '—', note: '未初始化', unwired: false, tone: 'warn' })
    expect(fields.find((field) => field.key === 'valid_draws')?.value).toBe('—')
  })

  it('后端未连接时保留连接状态文字与错误色调', () => {
    const fields = buildGlobalStatus({
      backend: { state: 'disconnected', lastError: '无法连接后端服务' },
      games: [],
      dataset: EMPTY_DATASET,
      now: NOW,
    })

    const backend = fields.find((field) => field.key === 'backend')
    expect(backend).toMatchObject({ value: '未连接', tone: 'error', unwired: false })
    expect(backend?.detail).toContain('无法连接后端服务')
  })
})
