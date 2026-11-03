/**
 * 开奖数据读取辅助。
 *
 * 后端 `GET /draws` 只提供按“期号升序 + 游标”的翻页，没有倒序/最新参数，
 * 因此要得到最新期必须从最早一页向后翻完。这里把该事实收敛到一处，并对
 * 游标循环做防护；页面不自行拼接“期号 + 1”猜测最新期。
 */

import { ApiError, apiGet, withQuery } from './client'
import type { Draw, DrawsResponse } from './types'

export const DRAW_PAGE_LIMIT = 200
const MAX_PAGES = 50

function contractError(code: string, message: string): ApiError {
  return new ApiError(
    0,
    { code, message, details: {}, request_id: '' },
    'unexpected',
  )
}

export async function fetchAllDraws(game = 'ssq', maxPages = MAX_PAGES): Promise<Draw[]> {
  const items: Draw[] = []
  let cursor: string | null = null
  const seen = new Set<string>()

  for (let page = 0; page < maxPages; page += 1) {
    const path = withQuery('/draws', {
      game,
      limit: DRAW_PAGE_LIMIT,
      cursor,
    })
    const payload: DrawsResponse = await apiGet<DrawsResponse>(path)
    items.push(...payload.items)
    if (!payload.next_cursor) return items
    if (seen.has(payload.next_cursor)) {
      throw contractError('CURSOR_LOOP', '后端分页游标出现循环，已停止读取')
    }
    seen.add(payload.next_cursor)
    cursor = payload.next_cursor
  }

  throw contractError(
    'TOO_MANY_PAGES',
    `开奖记录超过 ${maxPages} 页，已停止读取以避免不完整结果被当成全量`,
  )
}

/** 最新一期；无数据返回 null。 */
export function latestDraw(items: readonly Draw[]): Draw | null {
  return items.length ? items[items.length - 1] : null
}

/** 最近 n 期（保持升序）；n 小于等于 0 返回空数组。 */
export function recentDraws(items: readonly Draw[], count: number): Draw[] {
  if (count <= 0) return []
  return items.slice(-count)
}

/** 最近 n 期的区间；无数据返回 null。 */
export function recentRange(
  items: readonly Draw[],
  count: number,
): { start_issue: string; end_issue: string; count: number } | null {
  const selected = recentDraws(items, count)
  if (!selected.length) return null
  return {
    start_issue: selected[0].issue,
    end_issue: selected[selected.length - 1].issue,
    count: selected.length,
  }
}
