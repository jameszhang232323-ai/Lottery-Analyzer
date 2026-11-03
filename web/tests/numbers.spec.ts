import { describe, expect, it } from 'vitest'

import {
  isSeedHex,
  parseNumberList,
  parseOptionalInteger,
  parseOptionalNumberList,
  parseRangePair,
  parseTriple,
  randomSeedHex,
} from '../src/utils/numbers'

describe('号码与条件解析', () => {
  it('解析逗号/空格/中文分隔符，去重升序并忽略非法片段', () => {
    expect(parseNumberList('9, 1、5，3 3 abc')).toEqual([1, 3, 5, 9])
    expect(parseNumberList('')).toEqual([])
    expect(parseNumberList('1.5, 2')).toEqual([2])
  })

  it('空白可选列表返回 null（不限制），非空返回数组', () => {
    expect(parseOptionalNumberList('   ')).toBeNull()
    expect(parseOptionalNumberList('2,4')).toEqual([2, 4])
  })

  it('和值区间与三区个数只在完整时返回', () => {
    expect(parseRangePair('80', '160')).toEqual([80, 160])
    expect(parseRangePair('80', '')).toBeNull()
    expect(parseTriple('2,2,2')).toEqual([2, 2, 2])
    expect(parseTriple('2,2')).toBeNull()
  })

  it('可选整数空白返回 null，非整数返回 null', () => {
    expect(parseOptionalInteger('')).toBeNull()
    expect(parseOptionalInteger('3')).toBe(3)
    expect(parseOptionalInteger('3.5')).toBeNull()
  })
})

describe('随机种子', () => {
  it('生成 64 位十六进制且可被校验', () => {
    const seed = randomSeedHex()
    expect(seed).toHaveLength(64)
    expect(seed).toMatch(/^[0-9a-f]{64}$/)
    expect(isSeedHex(seed)).toBe(true)
  })

  it('拒绝长度或字符不合法的种子', () => {
    expect(isSeedHex('')).toBe(false)
    expect(isSeedHex('a'.repeat(63))).toBe(false)
    expect(isSeedHex('z'.repeat(64))).toBe(false)
    expect(isSeedHex('A'.repeat(64))).toBe(true)
  })
})
