import { readFileSync, readdirSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

/**
 * 静态契约测试。
 *
 * 不渲染 DOM：直接读取 src 下的路由、页面、样式与客户端源码，锁定任务 11
 * 的验收项——八页 + 登录页、真实接口调用、统一空状态/错误态、状态文字与颜色
 * 双表达、CSRF/401 处理，以及“浏览器不保存凭证”的边界。
 */

const srcRoot = new URL('../src/', import.meta.url)
const viewRoot = new URL('../src/views/', import.meta.url)

function read(url: URL): string {
  return readFileSync(url, 'utf8')
}

const allViewFiles = readdirSync(viewRoot)
  .filter((name) => name.endsWith('.vue'))
  .sort()
const businessViewFiles = allViewFiles.filter((name) => name !== 'LoginView.vue')

const sourceFiles = (readdirSync(srcRoot, { recursive: true }) as string[])
  .map((name) => name.replaceAll('\\', '/'))
  .filter((name) => /\.(ts|vue|css)$/.test(name))
  .sort()

const routerSource = read(new URL('router.ts', srcRoot))
const appSource = read(new URL('App.vue', srcRoot))
const clientSource = read(new URL('api/client.ts', srcRoot))
const authSource = read(new URL('stores/auth.ts', srcRoot))
const stylesSource = read(new URL('styles.css', srcRoot))
const loginSource = read(new URL('LoginView.vue', viewRoot))

function viewSource(name: string): string {
  return read(new URL(name, viewRoot))
}

describe('八页业务路由、登录页与页面骨架', () => {
  it('路由包含八个业务页与登录页，导航顺序与设计 4.2 一致', () => {
    const paths = [...routerSource.matchAll(/path: '([^']+)'/g)]
      .map((match) => match[1])
      .filter((path) => path.startsWith('/') && !path.includes(':'))

    expect(paths).toEqual([
      '/',
      '/draws',
      '/analysis',
      '/ai',
      '/strategies',
      '/backtests',
      '/candidates',
      '/settings',
      '/login',
    ])
    expect([...routerSource.matchAll(/nav: '/g)]).toHaveLength(8)
    expect(routerSource).toContain('designRef')
    expect(routerSource).toContain("name: 'login'")
    expect(routerSource).toContain('requiresAuth')
    expect(allViewFiles).toHaveLength(9)
    expect(businessViewFiles).toHaveLength(8)
  })

  it('每个业务页都有中文标题、口径说明与统一组件', () => {
    const navLabels = [...routerSource.matchAll(/nav: '([^']+)'/g)].map((match) => match[1])

    for (const file of businessViewFiles) {
      const source = viewSource(file)
      const heading = /<h1>([^<]+)<\/h1>/.exec(source)

      expect(heading, `${file} 缺少 h1`).not.toBeNull()
      expect(navLabels, `${file} 的标题应与导航一致`).toContain(heading?.[1])
      expect(source, `${file} 缺少口径说明`).toContain('view-scope')
      expect(source, `${file} 缺少口径标签`).toContain('view-scope-label')
      expect(source, `${file} 缺少 SectionCard`).toContain('<SectionCard')
      expect(source, `${file} 缺少 EmptyState`).toContain('<EmptyState')
      expect(source, `${file} 缺少 StatusPill`).toContain('<StatusPill')

      const cardCount = (source.match(/<SectionCard/g) ?? []).length
      expect(cardCount, `${file} 的核心区块过少`).toBeGreaterThanOrEqual(3)
    }
  })

  it('登录页有用户名/密码表单、错误提示与重定向保护', () => {
    expect(loginSource).toContain('<h1 id="login-title">登录</h1>')
    expect(loginSource).toContain('autocomplete="username"')
    expect(loginSource).toContain('type="password"')
    expect(loginSource).toContain('ErrorNote')
    expect(loginSource).toContain('redirect')
    expect(loginSource).toContain('!target.startsWith(\'//\')')
  })

  it('业务页不再有禁用占位按钮或“后端接口未接入”字样', () => {
    for (const file of businessViewFiles) {
      const source = viewSource(file)
      expect(source, `${file} 仍有字面 disabled 占位控件`).not.toMatch(/\sdisabled(?=[\s/>])/)
      expect(source, `${file} 仍有旧的未接入占位字样`).not.toContain('后端接口未接入')
    }
  })

  it('页面不出现占位数据类字样；密码输入框只允许登录与管理员创建用户', () => {
    const forbidden = ['示例', '演示数据', '模拟开奖', '假数据', '示范数据']
    const passwordAllowed = new Set(['views/LoginView.vue', 'views/SettingsView.vue'])

    for (const file of sourceFiles) {
      const source = read(new URL(file, srcRoot))
      for (const term of forbidden) {
        expect(source, `${file} 出现占位数据字样：${term}`).not.toContain(term)
      }
      if (!passwordAllowed.has(file)) {
        expect(source, `${file} 不应提供密码输入框`).not.toContain('type="password"')
      }
    }
  })
})

describe('八页真实接口清单', () => {
  const contracts: Array<[string, string[]]> = [
    ['OverviewView.vue', ['/sync-jobs', 'useDrawsStore', 'refreshAll', 'system.coverage']],
    ['DrawsView.vue', ['/draws', '/calendar', '/coverage', '/sync-jobs', '/draws/']],
    ['AnalysisView.vue', ['/snapshots', '/analysis-jobs', '/analyses/']],
    ['StrategiesView.vue', ['/snapshots', '/candidate-jobs', '/candidates/']],
    ['AiView.vue', ['/snapshots', '/ai-jobs', '/ai-runs/']],
    ['BacktestsView.vue', ['/snapshots', '/backtest-jobs', '/backtests/']],
    [
      'CandidateRecordsView.vue',
      ['/candidates/', '/lock', '/settlements', '/exports', '/artifacts/'],
    ],
    [
      'SettingsView.vue',
      ['/admin/users', '/admin/backups', '/admin/restores/preview', '/exports', '/artifacts/'],
    ],
  ]

  it('每个业务页调用对应的真实后端路径', () => {
    for (const [file, endpoints] of contracts) {
      const source = viewSource(file)
      for (const endpoint of endpoints) {
        expect(source, `${file} 缺少接口 ${endpoint}`).toContain(endpoint)
      }
    }
  })

  it('总览/统计/候选/回测复用开奖数据缓存与任务轮询，不伪造数据', () => {
    expect(read(new URL('stores/draws.ts', srcRoot))).toContain("fetchAllDraws")
    expect(read(new URL('api/draws.ts', srcRoot))).toContain("'/draws'")
    expect(read(new URL('stores/jobs.ts', srcRoot))).toContain('JOB_POLL_INTERVAL_MS = 2000')
    expect(read(new URL('components/TaskCenter.vue', srcRoot))).toContain('每 2 秒轮询')
    expect(read(new URL('components/EChart.vue', srcRoot))).toContain("'echarts/core'")
  })
})

describe('登录、CSRF 与错误边界', () => {
  it('会话初始化、登录与退出都走真实 /auth 接口', () => {
    expect(authSource).toContain("'/auth/me'")
    expect(authSource).toContain("'/auth/login'")
    expect(authSource).toContain("'/auth/logout'")
    expect(authSource).toContain('resetSessionState')
    expect(routerSource).toContain('await auth.ensureSession()')
  })

  it('CSRF 从 Cookie 读取并对非安全方法附带请求头', () => {
    expect(clientSource).toContain("CSRF_COOKIE_NAME = 'dajidali_csrf'")
    expect(clientSource).toContain("CSRF_HEADER_NAME = 'X-CSRF-Token'")
    expect(clientSource).toContain('isSafeMethod')
    expect(clientSource).toContain('csrfTokenFrom')
    expect(clientSource).toContain('credentials: \'same-origin\'')
  })

  it('401 触发全局处理器，网络错误与后端错误分开', () => {
    expect(clientSource).toContain('setUnauthorizedHandler')
    expect(clientSource).toContain('if (apiError.status === 401) unauthorizedHandler')
    expect(clientSource).toContain("'NETWORK_ERROR'")
    expect(clientSource).toContain('export function clientError')
    expect(appSource).toContain('auth.logout()')
    expect(appSource).toContain('syncError.kindLabel')
  })

  it('下载用鉴权 fetch + Blob + a[download]，不把令牌放 URL', () => {
    expect(clientSource).toContain('apiDownload')
    expect(clientSource).toContain('response.blob()')
    const downloadSource = read(new URL('utils/download.ts', srcRoot))
    expect(downloadSource).toContain('anchor.download')
    expect(downloadSource).toContain("anchor.rel = 'noopener'")
  })
})

describe('全局状态条与右侧 AI 解释区', () => {
  it('顶部状态条覆盖七项全局字段', () => {
    expect(appSource).toContain('global-status-bar')
    expect(appSource).toContain('aria-label="全局数据状态"')
    expect(appSource).toContain('system.globalStatus')
    expect(appSource).toContain('v-for="field in system.globalStatus"')
  })

  it('顶栏显示当前用户、角色、退出与管理员同步按钮', () => {
    expect(appSource).toContain('auth.displayName')
    expect(appSource).toContain('auth.role')
    expect(appSource).toContain('退出')
    expect(appSource).toContain('仅管理员可同步')
    expect(appSource).toContain('<TaskCenter')
  })

  it('右侧 AI 解释区可折叠且在窄屏折叠为浮层', () => {
    expect(appSource).toContain('ai-aside')
    expect(appSource).toContain('aria-expanded')
    expect(stylesSource).toContain('.ai-aside')
    expect(stylesSource).toContain('position: fixed')
    expect(stylesSource).toContain('@media (max-width: 1199px)')
  })
})

describe('响应式与设计 token', () => {
  it('红球、蓝球与状态色使用统一 CSS 变量', () => {
    expect(stylesSource).toContain('--color-red-ball: #b3261e;')
    expect(stylesSource).toContain('--color-blue-ball: #1c64b3;')
    expect(stylesSource).toContain('--color-status-ok:')
    expect(stylesSource).toContain('--color-status-warn:')
    expect(stylesSource).toContain('--color-status-error:')
    expect(stylesSource).toContain('--color-status-info:')
    expect(stylesSource).toContain('.ball--red')
    expect(stylesSource).toContain('background: var(--color-red-ball);')
    expect(stylesSource).toContain('background: var(--color-blue-ball);')
  })

  it('间距与圆角以变量统一，并覆盖窄屏横滚导航与任务中心', () => {
    for (const token of ['--space-1:', '--space-2:', '--space-3:', '--space-4:', '--space-6:']) {
      expect(stylesSource).toContain(token)
    }
    for (const token of ['--radius-sm:', '--radius-md:', '--radius-lg:']) {
      expect(stylesSource).toContain(token)
    }
    expect(stylesSource).toContain('@media (max-width: 899px)')
    expect(stylesSource).toContain('overflow-x: auto')
    expect(stylesSource).toContain('.task-center')
    expect(stylesSource).toContain('.chart-grid')
    expect(stylesSource).toContain('.login-shell')
  })
})

describe('API 客户端与凭证边界', () => {
  it('统一前缀、同源凭据与契约错误映射', () => {
    expect(clientSource).toContain("const API_BASE = '/api/v1'")
    expect(clientSource).toContain("credentials: 'same-origin'")
    expect(clientSource).toContain('export class ApiError')
    expect(clientSource).toContain('request_id')
    expect(clientSource).toContain("'NETWORK_ERROR'")
    expect(clientSource).toContain('export function apiGet')
    expect(clientSource).toContain('export function apiPost')
  })

  it('前端源码不通过浏览器存储保存凭证，也不写 Cookie', () => {
    for (const file of sourceFiles) {
      const source = read(new URL(file, srcRoot))
      // 只拦截真实调用；注释与界面文案可以声明“不使用浏览器存储”。
      expect(source, `${file} 使用了 localStorage`).not.toMatch(/\blocalStorage\s*[.[]/)
      expect(source, `${file} 使用了 sessionStorage`).not.toMatch(/\bsessionStorage\s*[.[]/)
      expect(source, `${file} 引用了 window.localStorage`).not.toMatch(/window\.localStorage\b/)
      expect(source, `${file} 直接写 Cookie`).not.toMatch(/document\.cookie\s*=/)
      expect(source, `${file} 出现 Bearer 凭据`).not.toMatch(/Bearer\s/)
    }
  })
})
