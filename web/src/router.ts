import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import AiView from './views/AiView.vue'
import AnalysisView from './views/AnalysisView.vue'
import BacktestsView from './views/BacktestsView.vue'
import CandidateRecordsView from './views/CandidateRecordsView.vue'
import DrawsView from './views/DrawsView.vue'
import OverviewView from './views/OverviewView.vue'
import SettingsView from './views/SettingsView.vue'
import StrategiesView from './views/StrategiesView.vue'

/**
 * 八页业务路由（设计文档 4.2）。页面全部先展示真实空状态，
 * 不放置任何示例开奖或示例成绩冒充真实数据。
 */
export const routes: RouteRecordRaw[] = [
  { path: '/', name: 'overview', component: OverviewView, meta: { title: '总览' } },
  { path: '/draws', name: 'draws', component: DrawsView, meta: { title: '开奖数据' } },
  { path: '/analysis', name: 'analysis', component: AnalysisView, meta: { title: '统计分析' } },
  { path: '/ai', name: 'ai', component: AiView, meta: { title: 'AI 分析' } },
  { path: '/strategies', name: 'strategies', component: StrategiesView, meta: { title: '策略实验' } },
  { path: '/backtests', name: 'backtests', component: BacktestsView, meta: { title: '历史回测' } },
  {
    path: '/candidates',
    name: 'candidates',
    component: CandidateRecordsView,
    meta: { title: '候选记录' },
  },
  { path: '/settings', name: 'settings', component: SettingsView, meta: { title: '设置与任务' } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? '总览'
  document.title = `${title} · 大吉大利`
})

export default router
