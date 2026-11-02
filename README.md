# 大吉大利 · 福彩数据分析工作台

双色球（近五年）数据获取、统计分析、AI 辅助研判、候选生成、历史回测、开奖前锁定与开奖后核对的 B/S 版工作台。

- 代码仓库：<https://github.com/jameszhang232323-ai/Lottery-Analyzer>
- 当前阶段：**环境与工程骨架**（对应实施计划任务 00/01 的环境部分），业务功能尚未实现。
- 技术基线：Python 3.14 + FastAPI + SQLAlchemy/Alembic；Vue 3 + TypeScript + Vite + ECharts；本机 SQLite、服务器 PostgreSQL。

## 文档

| 文档 | 说明 |
|---|---|
| [docs/README.md](docs/README.md) | 文档入口 |
| [B/S 版详细设计](docs/superpowers/specs/2026-11-02-dajidali-bs-design.md) | 当前设计基线 V1.1 |
| [B/S 版实施计划](docs/superpowers/plans/2026-11-02-dajidali-bs-implementation-plan.md) | 13 个任务、46 开发人日 |

## 目录

```text
backend/          FastAPI 后端与计算核心
  src/dajidali/   领域、引擎、数据、服务、任务、安全、API
  tests/          单元、契约、集成与安全测试
web/              Vue 3 + TypeScript 前端（八页界面）
docs/             设计与验收文档
```

## 快速开始

### 后端

```powershell
# 首次：创建虚拟环境并安装依赖（Python 3.12+）
python -m venv backend/.venv
backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"

# 运行 API（默认 http://127.0.0.1:8000）
backend\.venv\Scripts\python.exe -m uvicorn dajidali.api.app:app --app-dir backend/src --reload

# 检查
backend\.venv\Scripts\python.exe -m pytest backend/tests
backend\.venv\Scripts\python.exe -m ruff check backend/src backend/tests
```

健康检查：`GET /api/v1/health/live`、`GET /api/v1/health/ready`。

### 前端

```powershell
cd web
pnpm install
pnpm dev          # http://127.0.0.1:5173 ，/api 代理到 8000
pnpm typecheck
pnpm build
```

## 约定

- 浏览器只调用 `/api/v1`，不直连数据库、数据源与 AI 服务。
- 后端密钥只在服务端保管，前端资源、localStorage、URL 与日志不得出现密钥。
- 领域对象不可变；计算核心不导入 FastAPI/SQLAlchemy，不自行联网或读数据库。
- 本仓库不包含真实开奖数据导出、用户密钥或任何付费接口凭证。
