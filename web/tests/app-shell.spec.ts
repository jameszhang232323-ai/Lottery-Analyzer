import { readFileSync, readdirSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

/**
 * 静态契约测试。
 *
 * 不渲染 DOM：直接读取 src 下的路由、页面、样式与客户端源码，锁定任务 01/11
 * 中容易回退的验收项——八页齐全、真实空状态、统一组件、禁用按钮标注、
 * 状态文字与颜色 token、以及“浏览器不保存凭证”的边界。
 */

const srcRoot = new URL('../src/', import.meta.url)
const viewRoot = new URL('../src/views/', import.meta.url)

function read(url: URL): string {
  return readFileSync(url, 'utf8')
}

const viewFiles = readdirSync(viewRoot)
  .filter((name) => name.endsWith('.vue'))
  .sort()

const sourceFiles = (readdirSync(srcRoot, { recursive: true }) as string[])
  .map((name) => name.replaceAll('\\', '/'))
  .filter((name) => /\.(ts|vue|css)$/.test(name))
  .sort()

const routerSource = read(new URL('router.ts', srcRoot))
const appSource = read(new URL('App.vue', srcRoot))
const clientSource = read(new URL('api/client.ts', srcRoot))
const stylesSource = read(new URL('styles.css', srcRoot))

describe('八页路由与页面骨架', () => {
  it('路由包含八个业务页且顺序与设计 4.2 一致', () => {
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
    ])
    expect([...routerSource.matchAll(/nav: '/g)]).toHaveLength(8)
    expect(routerSource).toContain('designRef')
    expect(viewFiles).toHaveLength(8)
  })

  it('每个页面都有中文标题、口径说明与统一组件', () => {
    const navLabels = [...routerSource.matchAll(/nav: '([^']+)'/g)].map((match) => match[1])

    for (const file of viewFiles) {
      const source = read(new URL(file, viewRoot))
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

  it('所有禁用按钮都标注“后端接口未接入”', () => {
    for (const file of viewFiles) {
      const source = read(new URL(file, viewRoot))
      const buttons = source.match(/<button\b[^>]*>/g) ?? []
      const literalDisabled = buttons.filter((tag) => /\sdisabled(?=[\s/>])/.test(tag))

      expect(buttons.length, `${file} 未画出任何操作`).toBeGreaterThan(0)
      expect(literalDisabled.length, `${file} 没有禁用占位按钮`).toBeGreaterThan(0)

      for (const tag of literalDisabled) {
        expect(tag, `${file} 的禁用按钮缺少接口未接入标注`).toContain('后端接口未接入')
      }
    }
  })

  it('页面不出现占位数据类字样，且不提供密码输入框', () => {
    const forbidden = ['示例', '演示数据', '模拟开奖', '假数据', '示范数据']

    for (const file of sourceFiles) {
      const source = read(new URL(file, srcRoot))
      for (const term of forbidden) {
        expect(source, `${file} 出现占位数据字样：${term}`).not.toContain(term)
      }
      expect(source, `${file} 不应提供密码输入框`).not.toContain('type="password"')
    }
  })
})

describe('全局状态条与右侧 AI 解释区', () => {
  it('顶部状态条覆盖七项全局字段', () => {
    expect(appSource).toContain('global-status-bar')
    expect(appSource).toContain('aria-label="全局数据状态"')
    expect(appSource).toContain('system.globalStatus')
    expect(appSource).toContain('v-for="field in system.globalStatus"')
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

  it('间距与圆角以变量统一，并覆盖窄屏横滚导航', () => {
    for (const token of ['--space-1:', '--space-2:', '--space-3:', '--space-4:', '--space-6:']) {
      expect(stylesSource).toContain(token)
    }
    for (const token of ['--radius-sm:', '--radius-md:', '--radius-lg:']) {
      expect(stylesSource).toContain(token)
    }
    expect(stylesSource).toContain('@media (max-width: 899px)')
    expect(stylesSource).toContain('overflow-x: auto')
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
