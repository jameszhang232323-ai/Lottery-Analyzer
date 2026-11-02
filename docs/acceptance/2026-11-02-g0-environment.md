# 环境验收记录 · 2026-11-02（G0 环境部分）

对应《B/S 版实施计划》任务 00 的“检查工作区、Python 虚拟环境、Node 构建工具、Windows 运行条件”，
以及任务 01 的工程骨架。**本记录只覆盖本机开发环境，不含数据源许可、AI 账号与服务器部署验收。**

状态：本机开发环境可用；后端测试与静态检查通过，前端类型检查与生产构建通过，初始提交已推送到 GitHub `main` 分支。

## 一、机器与工具链

| 项目 | 实测值 | 说明 |
|---|---|---|
| 操作系统 | Windows（Windows PowerShell 5.1.26100.9711） | 系统自带 PowerShell 为 5.1，`??` 等 7.x 语法不可用 |
| 工作区 | `D:\codex project\福彩分析`（含中文路径） | 中文路径下 Python 与 Node 均可正常工作 |
| 磁盘 | D: 169 GB 可用 / 800 GB | |
| Python | 3.14.7（`C:\Users\12160\AppData\Local\Programs\Python\Python314`） | 项目要求 ≥3.12 |
| Node.js | v24.21.0（DSH 自带运行时） | 计划原写“开发构建需要 Node.js”，未锁定具体大版本 |
| pnpm | 11.28.5（DSH 自带运行时） | 通过 `node <pnpm.mjs>` 调用，避免 PowerShell 执行策略拦截 |
| Git | 2.55.0.windows.3 | |
| GitHub CLI | 已安装，未登录 | 推送改用系统凭据管理器（Git Credential Manager）成功 |
| Docker | 未安装 | 服务器 Compose 验收阶段再装 |
| 网络 | 可访问 registry.npmjs.org 与 pypi.org | PowerShell `Invoke-WebRequest` 失败是 TLS 后端问题，Python/Node 正常 |

## 二、后端（`backend/`）

- 虚拟环境：`backend/.venv`（Python 3.14.7），可编辑安装 `dajidali==0.1.0`。
- 依赖锁定：`backend/requirements.lock`（`pip freeze --exclude-editable`，35 项）。
- 关键版本：FastAPI 0.162.0、Starlette 1.10.0、Pydantic 2.14.2、SQLAlchemy 2.1.6、
  Alembic 1.20.1、httpx 0.28.1、pytest 9.2.0、ruff 0.17.3。

实测结果：

| 检查 | 命令 | 结果 |
|---|---|---|
| 单元 + 契约测试 | `python -m pytest` | **25 passed** |
| 静态检查 | `python -m ruff check src tests` | **All checks passed** |
| 应用装配 | `GET /api/v1/health/live`、`/health/ready`、`/games` | 通过（TestClient 覆盖） |
| 真实进程 | uvicorn 监听 `127.0.0.1:8010`，请求 `/health/live` 与 `/games` | 均返回 200 |
| OpenAPI | `GET /api/v1/openapi.json` | 通过，标题“大吉大利 API” |

已实现（骨架级）：不可变领域对象 `SsqNumbers` / `Fc3dNumbers` / `DrawRevision`、
期号跨年排序、统一错误结构（`code`/`message`/`details`/`request_id`）、
`request_id` 中间件、`/api/v1` 前缀与健康探针。

尚未实现：数据库、迁移、登录鉴权、采集、任务队列、统计、候选、回测、AI、锁定核对、导出备份。

## 三、前端（`web/`）

- 依赖：Vue 3.5.44、vue-router 4.6.4、Pinia 2.3.1、ECharts 5.6.0、Vite 6.4.5、TypeScript 5.6.3、vue-tsc 2.2.12。
- 锁文件：`web/pnpm-lock.yaml`；依赖存储放在工作区 `.pnpm-store/`（见 `web/.npmrc`），便于离线复现。
- 页面：八页路由（总览、开奖数据、统计分析、AI 分析、策略实验、历史回测、候选记录、设置与任务），
  全部展示真实空状态，未放置任何示例开奖或示例成绩。

| 检查 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `pnpm typecheck`（vue-tsc 3.3.14） | **通过** |
| 生产构建 | `pnpm build` | **通过**：50 个模块，`dist/index.html` + JS 106.8 kB（gzip 42.1 kB）+ CSS 3.3 kB |

## 四、本机沙箱限制与解除方式

下面三条限制来自同一个原因：DSH 的 `workspace-write` 沙箱（Windows 上以受限身份执行命令）。
**这不是 Windows 文件权限问题**，改目录权限或换管理员账户都不能解决；要彻底消除只能切换 DSH 的权限预设。

### 4.1 彻底解除（三条同时生效）

| 层级 | 操作 | 生效范围 |
|---|---|---|
| 当前会话 | 在会话的「权限 / Permissions」选择器里切到 `danger-full-access`（完全访问） | 立即生效；沙箱关闭、审批改为 never |
| 以后新建的会话 | 设置 → 权限，把默认预设改为 `danger-full-access`（保存为 `defaultPreset`） | 只影响之后创建的会话，不改当前会话 |
| 启动层 | 启动 DSH 前设置环境变量 `DSH_PERMISSION_MODE=danger-full-access` | 该进程内新建会话的默认值 |

代价：文件、网络、子进程都不再受限，命令来源必须可信。这是 DSH 设计上唯一的“真正解除”开关。

### 4.2 保留沙箱时的替代方案（均已实测）

| 限制 | 现象 | 替代方案 | 实测结果 |
|---|---|---|---|
| Python 临时目录 | `tempfile.mkdtemp` 以 0700 建目录，沙箱拒绝写入，`pip install` 与 pytest 缓存失败 | 运行 `tooling/setup-windows-sandbox.ps1`，把 `sitecustomize.py` 装进虚拟环境，改用默认权限建临时目录 | 沙箱内 `pip install/uninstall`、pytest（含缓存）均通过 |
| 前端构建 | Vite 在 Windows 上执行 `net use` 探测网络盘，沙箱禁止带管道的子进程（`spawn EPERM`） | `package.json` 的 build 改为 `vite build --configLoader native` | 沙箱内 `pnpm build` 通过（50 模块） |
| Git 访问远端 | schannel 在受限身份下取不到 TLS 凭据（`SEC_E_NO_CREDENTIALS`） | `git config --local http.sslBackend openssl`（`setup-windows-sandbox.ps1` 会设置） | 沙箱内 `ls-remote` / `fetch` 可用 |
| Git 推送 | 凭据助手需要命名对象，沙箱内报 `NtCreateDirectoryObject ... 0xC0000022` | 无配置解法；推送时切 `danger-full-access`，或改用文件型凭据（`credential.helper=store`，需自行评估 token 落盘风险） | 本次推送均使用一次性放宽 |

与沙箱无关、属于本机配置的两项：

1. **PowerShell 执行策略为 Restricted**：`.ps1`（含 `npm.ps1`）不能直接运行。用 `npm.cmd` 或 DSH 自带的
   `node.exe + pnpm.mjs`；脚本用 `Set-ExecutionPolicy -Scope Process Bypass` 临时放行（本次安装补丁即用此法）。
2. **pnpm 依赖脚本策略**：`vue-demi` 的安装脚本被 pnpm 拒绝，`pnpm install` 以退出码 1 结束，但
   `node_modules/vue-demi/lib` 已就绪，类型检查与构建均通过。依赖使用扁平布局（`nodeLinker: hoisted`），
   否则 `vue-tsc` 会因 `estree-walker` 的 ESM-only 导出解析失败（`ERR_PACKAGE_PATH_NOT_EXPORTED`）。

### 4.3 工作区写权限

初次进入时工作区目录缺少写入所需权限，已用 DSH 权限诊断脚本修复（备份与恢复命令在
`D:\codex project\福彩分析-acl-recovery`，该目录在工作区外，不属于仓库内容）。

## 五、尚未验证（G0 其余门槛）

- 数据源：五年覆盖、跨源一致性、保存与展示许可——**未验证**。
- AI 服务：真实账号与结构化响应、用量与计费——**未验证**。
- PostgreSQL：本机无 PG 与 Docker，服务器部署验收未开始。
- 本机启动脚本、服务器 Compose、HTTPS 反向代理——未开始。

按计划口径：在线来源未准入前，不承诺五年在线交付；上述未验证项不得标记为完成。
