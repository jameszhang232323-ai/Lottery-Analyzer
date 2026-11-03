import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import AiView from './views/AiView.vue'
import AnalysisView from './views/AnalysisView.vue'
import BacktestsView from './views/BacktestsView.vue'
import CandidateRecordsView from './views/CandidateRecordsView.vue'
import DrawsView from './views/DrawsView.vue'
import LoginView from './views/LoginView.vue'
import OverviewView from './views/OverviewView.vue'
import SettingsView from './views/SettingsView.vue'
import StrategiesView from './views/StrategiesView.vue'
import { useAuthStore } from './stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    /** 浏览器标题与页面标题共用的中文名。 */
    title?: string
    /** 左侧导航文字；只有业务页设置，登录页与重定向路由不设置。 */
    nav?: string
    /** 导航顺序，与设计 4.2 的页面清单顺序一致。 */
    order?: number
    /** 设计文档对应条目，便于评审时逐页核对。 */
    designRef?: string
    /** 页面职责摘要。 */
    summary?: string
    /** 业务页要求已登录；登录页显式设为 false。 */
    requiresAuth?: boolean
    /** 登录页使用独立布局，不渲染导航、状态条与任务中心。 */
    layout?: 'blank'
  }
}

/** 八页业务路由（设计文档 4.2）加登录页。 */
export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'overview',
    component: OverviewView,
    meta: {
      title: '总览',
      nav: '总览',
      order: 1,
      designRef: '4.2 总览',
      summary: '最新结果、数据质量、已锁定批次与近期实验摘要',
      requiresAuth: true,
    },
  },
  {
    path: '/draws',
    name: 'draws',
    component: DrawsView,
    meta: {
      title: '开奖数据',
      nav: '开奖数据',
      order: 2,
      designRef: '4.2 开奖数据',
      summary: '期号与日期筛选、号码表、来源详情、修订时间线与缺期列表',
      requiresAuth: true,
    },
  },
  {
    path: '/analysis',
    name: 'analysis',
    component: AnalysisView,
    meta: {
      title: '统计分析',
      nav: '统计分析',
      order: 3,
      designRef: '4.2 统计分析',
      summary: '窗口筛选与频次、遗漏、和值、奇偶、分区等统计口径',
      requiresAuth: true,
    },
  },
  {
    path: '/ai',
    name: 'ai',
    component: AiView,
    meta: {
      title: 'AI 分析',
      nav: 'AI 分析',
      order: 4,
      designRef: '4.2 AI分析',
      summary: '条件区、事实结果、AI 报告与引用、发送资料预览',
      requiresAuth: true,
    },
  },
  {
    path: '/strategies',
    name: 'strategies',
    component: StrategiesView,
    meta: {
      title: '策略实验',
      nav: '策略实验',
      order: 5,
      designRef: '4.2 策略实验',
      summary: 'S0/S1 参数、结构条件、候选表与随机对照入口',
      requiresAuth: true,
    },
  },
  {
    path: '/backtests',
    name: 'backtests',
    component: BacktestsView,
    meta: {
      title: '历史回测',
      nav: '历史回测',
      order: 6,
      designRef: '4.2 历史回测',
      summary: '时间划分、参数冻结、任务进度、比较表与逐期结果',
      requiresAuth: true,
    },
  },
  {
    path: '/candidates',
    name: 'candidates',
    component: CandidateRecordsView,
    meta: {
      title: '候选记录',
      nav: '候选记录',
      order: 7,
      designRef: '4.2 候选记录',
      summary: '草稿、锁定、待开奖、已核对与修订待重算',
      requiresAuth: true,
    },
  },
  {
    path: '/settings',
    name: 'settings',
    component: SettingsView,
    meta: {
      title: '设置与任务',
      nav: '设置与任务',
      order: 8,
      designRef: '4.2 设置与任务',
      summary: '数据源、AI、费用限制、备份、版本、日志与任务队列',
      requiresAuth: true,
    },
  },
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: {
      title: '登录',
      requiresAuth: false,
      layout: 'blank',
    },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.requiresAuth === false) {
    if (!auth.initialized) await auth.ensureSession()
    if (to.name === 'login' && auth.isAuthenticated) {
      const value = to.query.redirect
      const target = Array.isArray(value) ? value[0] : value
      return {
        path: typeof target === 'string' && target.startsWith('/') && !target.startsWith('//') ? target : '/',
      }
    }
    return true
  }

  await auth.ensureSession()
  if (!auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? '大吉大利'
  document.title = `${title} · 大吉大利`
})

export default router
