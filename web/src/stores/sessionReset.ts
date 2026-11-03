/**
 * 用户切换/退出时清空所有内存态业务数据。
 *
 * 任务、候选与开奖缓存都可能带有上一个会话的上下文；退出或 401 后必须清空，
 * 避免切换用户时残留他人页面数据。浏览器存储不参与。
 */

import { useCandidatesStore } from './candidates'
import { useDrawsStore } from './draws'
import { useJobsStore } from './jobs'
import { useSystemStore } from './system'

export function resetSessionState(): void {
  useCandidatesStore().clear()
  useDrawsStore().clear()
  useJobsStore().clearAll()
  useSystemStore().reset()
}
