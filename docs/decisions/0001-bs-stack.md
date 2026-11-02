# 决策 0001 · B/S 技术栈与版本锁定

- 决策日期：2026-11-02
- 状态：已采纳（本机开发环境已验证；服务器环境未验证）
- 依据：[B/S 版详细设计](../../superpowers/specs/2026-11-02-dajidali-bs-design.md) 3.1、3.4；
  [环境验收记录](../../acceptance/2026-11-02-g0-environment.md)

## 决策

首版采用 **Python 3.14 + FastAPI + SQLAlchemy 2.x + Alembic**，前端 **Vue 3 + TypeScript + Vite + ECharts**，
本机 SQLite、服务器 PostgreSQL，浏览器只访问同源 `/api/v1`。计算核心是**独立 Python 包**（`engine/`），不依赖
Web 框架与数据库，可单独测试与复算。

## 理由

1. **契约可执行**：领域对象与引擎用纯 Python 表达，测试不需要起服务；同一套代码可供 worker 与 CLI 复用。
2. **两类部署共用一个后端**：SQLAlchemy 统一模型、Alembic 统一迁移，SQLite 与 PG 的差异集中在连接与锁策略。
3. **前端不复制算法**：页面只渲染后端事实与任务状态，OpenAPI 生成类型（当前阶段为手写契约，任务 11 替换）。
4. **依赖可锁定**：后端 `backend/requirements.lock`（`pip freeze --exclude-editable`），前端 `web/pnpm-lock.yaml`。

## 版本基线（2026-11-02 实测）

| 组件 | 版本 | 约束 |
|---|---|---|
| Python | 3.14.7 | `requires-python >= 3.12` |
| FastAPI / Starlette | 0.162.0 / 1.10.0 | 仅 API 层使用 |
| Pydantic | 2.14.2 | 输入输出模型 |
| SQLAlchemy / Alembic | 2.1.6 / 1.20.1 | 声明式 2.0 风格 |
| pytest / ruff | 9.2.0 / 0.17.3 | 测试与静态检查 |
| Node.js | 24.21.0（DSH 自带） | 仅开发构建需要，运行端不需要 |
| pnpm | 11.28.5 | `nodeLinker: hoisted`，`--configLoader native` 构建 |
| Vue / Vite / TypeScript | 3.5.44 / 8.3.5 / 5.9.3 | 前端 |
| ECharts / Pinia / vue-router | 5.6.0 / 2.3.1 / 4.6.4 | 图表、状态、路由 |

## 依赖与构建约定

- 新增后端依赖必须同时更新 `requirements.lock`，并在 PR 说明用途与许可证。
- 前端依赖改动必须提交 `pnpm-lock.yaml`；依赖安装脚本默认拒绝，只有确实需要构建的包加入
  `pnpm-workspace.yaml` 的 `onlyBuiltDependencies`。
- 不使用 CDN：字体、图表与脚本全部随构建产物发布。
- 计算核心只允许标准库；引入 NumPy/Pandas 需单独评估并记录在案（首版不需要）。

## 未决事项

- 服务器 PostgreSQL 版本与连接池参数（任务 12 前定）。
- 前端类型是否由 OpenAPI 自动生成（任务 11 决定；当前为手写契约 + 契约测试）。
- DSH 沙箱下的本机开发限制（临时目录、Vite 子进程、Git 推送凭据）已在环境验收记录中给出替代方案，
  与产品运行环境无关。
