/**
 * 创建任务后统一登记到任务中心，并把 job_id 写入 URL query。
 *
 * 只写 job_id，不写任何令牌或结果；刷新页面后任务中心按 id 重新读取后端状态。
 */
import { useRoute, useRouter } from 'vue-router'

import type { JobStatus } from '../api/types'
import { useJobsStore } from '../stores/jobs'

export interface JobLaunchInput {
  id: number
  kind: string
  label?: string
  status?: JobStatus | string
}

export function useJobLauncher() {
  const jobs = useJobsStore()
  const route = useRoute()
  const router = useRouter()

  function launch(input: JobLaunchInput): void {
    jobs.track(input)
    const current = String(route.query.job ?? '')
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean)
    const next = Array.from(new Set([...current, String(input.id)]))
    void router.replace({ query: { ...route.query, job: next.join(',') } })
  }

  return { launch }
}
