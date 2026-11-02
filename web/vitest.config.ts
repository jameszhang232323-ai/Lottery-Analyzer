// 前端单元测试只覆盖纯逻辑（状态派生、错误映射、静态契约），不加载 DOM。
// 配置刻意不 import 任何包：沙箱内 Vite 打包配置会触发 Windows 子进程探测，
// 而纯对象导出的配置可被直接加载；测试使用 worker 线程池，避免 fork 进程。
export default {
  test: {
    environment: 'node',
    include: ['tests/**/*.spec.ts'],
    pool: 'threads',
    clearMocks: true,
    reporters: ['default'],
  },
}
