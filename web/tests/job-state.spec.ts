import { describe, expect, it } from 'vitest'

import type { CoverageResponse } from '../src/api/types'
import {
  describeCandidateStatus,
  describeCoverage,
  describeDrawQuality,
  describeJobProgress,
  describeJobStatus,
  formatJsonValue,
  formatRatio,
  isTerminalJobStatus,
  jobKindLabel,
  roleLabel,
  windowLabel,
} from '../src/stores/selectors'

function coverage(overrides: Partial<CoverageResponse> = {}): CoverageResponse {
  return {
    game: 'ssq',
    start_issue: '2025001',
    end_issue: '2025100',
    status: 'complete',
    complete: true,
    calendar_known: true,
    calendar_covers_range: true,
    expected_count: 100,
    acquired_count: 100,
    analyzable_count: 100,
    official_verified_count: 0,
    missing_count: 0,
    disputed_count: 0,
    closed_count: 0,
    delayed_count: 0,
    cancelled_count: 0,
    unexpected_count: 0,
    missing_issues: [],
    disputed_issues: [],
    closed_issues: [],
    delayed_issues: [],
    cancelled_issues: [],
    unexpected_issues: [],
    calendar_basis: [],
    reasons: [],
    ...overrides,
  }
}

describe('任务状态机', () => {
  it('终态只包含成功、失败、取消与中断', () => {
    for (const status of ['succeeded', 'failed', 'cancelled', 'interrupted']) {
      expect(isTerminalJobStatus(status), status).toBe(true)
    }
    for (const status of ['queued', 'running', 'paused', 'unknown']) {
      expect(isTerminalJobStatus(status), status).toBe(false)
    }
  })

  it('每个已知状态都有文字与色调，未知状态不伪装成功', () => {
    expect(describeJobStatus('queued')).toMatchObject({ label: '排队中', tone: 'info' })
    expect(describeJobStatus('running')).toMatchObject({ label: '运行中', tone: 'info' })
    expect(describeJobStatus('paused')).toMatchObject({ label: '已暂停', tone: 'warn' })
    expect(describeJobStatus('succeeded')).toMatchObject({ label: '已完成', tone: 'ok' })
    expect(describeJobStatus('failed')).toMatchObject({ label: '失败', tone: 'error' })
    expect(describeJobStatus('cancelled')).toMatchObject({ label: '已取消', tone: 'neutral' })
    expect(describeJobStatus('interrupted')).toMatchObject({ label: '已中断', tone: 'warn' })
    expect(describeJobStatus('weird')).toMatchObject({ label: '未知状态', tone: 'neutral' })
  })

  it('任务类型有中文标签', () => {
    expect(jobKindLabel('sync.run')).toBe('数据同步')
    expect(jobKindLabel('analysis.run')).toBe('统计分析')
    expect(jobKindLabel('candidate.generate')).toBe('候选生成')
    expect(jobKindLabel('backtest.run')).toBe('历史回测')
    expect(jobKindLabel('ai.analyze')).toBe('AI 分析')
    expect(jobKindLabel('custom.kind')).toBe('custom.kind')
  })
})

describe('任务进度解析', () => {
  it('优先使用 processed_periods/total_periods，并夹取 0—100%', () => {
    expect(describeJobProgress({ phase: 'walk_forward', processed_periods: 3, total_periods: 10 })).toMatchObject({
      text: 'walk_forward 3/10（30%）',
      percent: 30,
      phase: 'walk_forward',
    })
    expect(describeJobProgress({ completed: 200, total: 100 })).toMatchObject({ percent: 100 })
    expect(describeJobProgress({ completed: -1, total: 10 })).toMatchObject({ percent: 0 })
  })

  it('只有完成量时不给百分比；无字段时显示 —', () => {
    const partial = describeJobProgress({ processed_periods: 7 })
    expect(partial.percent).toBeNull()
    expect(partial.text).toContain('7')
    expect(describeJobProgress(null)).toEqual({ text: '—', percent: null, phase: '' })
  })

  it('回退到 checkpoint，并识别 message 文本', () => {
    expect(describeJobProgress(null, { processed_periods: 2, total_periods: 4 }).percent).toBe(50)
    expect(describeJobProgress({ message: '等待租约' }).text).toBe('等待租约')
  })
})

describe('开奖质量、覆盖、候选与格式化', () => {
  it('质量状态文字与色调完整', () => {
    expect(describeDrawQuality('verified')).toMatchObject({ label: '已核验', tone: 'ok' })
    expect(describeDrawQuality('unverified')).toMatchObject({ label: '未核验', tone: 'info' })
    expect(describeDrawQuality('disputed')).toMatchObject({ label: '存在冲突', tone: 'error' })
    expect(describeDrawQuality('other')).toMatchObject({ label: '未评估', tone: 'neutral' })
  })

  it('覆盖清单区分完整、不完整、冲突与日历未知', () => {
    expect(describeCoverage(null)).toMatchObject({ label: '未查询', tone: 'neutral' })
    expect(describeCoverage(coverage())).toMatchObject({ label: '范围完整', tone: 'ok' })
    expect(
      describeCoverage(coverage({ complete: false, acquired_count: 90, missing_count: 10 })),
    ).toMatchObject({ label: '范围不完整', tone: 'warn' })
    expect(describeCoverage(coverage({ disputed_count: 2 }))).toMatchObject({
      label: '存在冲突',
      tone: 'error',
    })
    expect(describeCoverage(coverage({ calendar_known: false }))).toMatchObject({
      label: '日历未知',
      tone: 'warn',
    })
  })

  it('候选状态覆盖生命周期', () => {
    expect(describeCandidateStatus('draft')).toMatchObject({ label: '草稿', tone: 'neutral' })
    expect(describeCandidateStatus('locked')).toMatchObject({ label: '已锁定', tone: 'info' })
    expect(describeCandidateStatus('awaiting_result')).toMatchObject({ label: '待开奖' })
    expect(describeCandidateStatus('settled')).toMatchObject({ label: '已核对', tone: 'ok' })
    expect(describeCandidateStatus('archived')).toMatchObject({ label: '已归档' })
    expect(describeCandidateStatus('x')).toMatchObject({ label: '未知状态' })
  })

  it('角色、窗口与比率格式化', () => {
    expect(roleLabel('admin')).toBe('管理员')
    expect(roleLabel('analyst')).toBe('分析员')
    expect(roleLabel('viewer')).toBe('访客')
    expect(windowLabel(30)).toBe('30 期')
    expect(windowLabel('all')).toBe('全部')
    expect(formatRatio(0.123456)).toBe('0.1235')
    expect(formatRatio(null)).toBe('—')
  })

  it('未知 JSON 值格式化不抛异常', () => {
    expect(formatJsonValue(null)).toBe('—')
    expect(formatJsonValue('text')).toBe('text')
    expect(formatJsonValue({ a: 1 })).toBe('{"a":1}')
  })
})
