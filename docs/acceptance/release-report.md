# 发布验收报告 · 2026-11-03

- 对应计划：[B/S 版实施计划](../superpowers/plans/2026-11-02-dajidali-bs-implementation-plan.md)
- 对应设计：[B/S 版详细设计](../superpowers/specs/2026-11-02-dajidali-bs-design.md)
- 代码仓库：<https://github.com/jameszhang232323-ai/Lottery-Analyzer>（`main`）
- 结论：**代码与自动化验收完成；在线交付门槛未通过**——真实数据源、真实 AI、PostgreSQL/Docker 部署尚未实测。

## 一、验证基线

| 项 | 结果 | 命令 |
|---|---|---|
| 后端静态检查 | 通过 | `backend/.venv/Scripts/python.exe -m ruff check src tests` |
| 后端测试 | **663 passed, 2 skipped** | `.venv\Scripts\python.exe -m pytest` |
| 前端类型检查 | 通过 | `node <node> <pnpm> typecheck` |
| 前端构建 | 通过（主包 252 kB / gzip 83 kB，ECharts 分块） | `... build` |
| 前端单元测试 | **85 passed** | `... test:unit` |
| 一键门禁 | 通过 | `powershell -ExecutionPolicy Bypass -File tooling/check.ps1` |
| 端到端烟测 | 通过 | `python tooling/smoke_local.py ...` |

2 个 skip 的原因：无 PostgreSQL 实例（两库迁移）；Windows 沙箱禁止 `os.symlink`（符号链接逃逸用例）。

端到端烟测实测输出（真实 API + worker 进程，录制源）：

```text
[OK] health/ready
[OK] auth/me — admin
[OK] sync.run — fetched=39, conflicts=0
[OK] coverage — acquired=39
[OK] snapshots — id=1, members=39
[OK] analysis.run — facts=840
[OK] candidate.generate — batch=1, lines=5, status=draft
```

## 二、功能矩阵

| 领域 | 实现 | 自动化验证 | 真实环境验证 |
|---|---|---|---|
| 领域对象与号码规则 | 完成 | 单元测试（红球互异/范围、3D 前导零、期号跨年） | — |
| 数据层与迁移 | 完成（0001—0005，24+ 表） | 集成测试 + `alembic upgrade/downgrade` | **SQLite 已验；PG 未验** |
| 修订链与快照 | 完成 | 集成测试（旧快照不随修订变化、同成员同哈希） | SQLite |
| 登录、会话、CSRF、密钥 | 完成 | security 34 项（越权、CSRF、掩码、明文不入库） | 本机 |
| 统计引擎 | 完成（30/100/300/all） | 单元测试含手算样例与截断遗漏 | 本机 |
| 随机流与候选 | 完成（sha256-counter-v1、S0/S1） | 黄金样例 + 跨平台复现测试 | 本机 |
| 锁定与奖级 | 完成（2026014 新规、福运奖） | 24 项规则 + 10 项锁定 + 4 项并发 | 本机（SQLite） |
| 结算与修订 | 完成 | 幂等、更正追加、争议冻结 | SQLite |
| 回测 | 完成（60/20/20、B0/B1、bootstrap） | 59 项（防泄漏、封存、区间） | 本机 |
| AI | 完成（证据包、校验、额度、`ai.analyze`） | 83 项（FakeProvider） | **真实模型未验** |
| 采集 | 完成可先行部分（适配协议、录制源） | 44 项 | **真实来源未验** |
| 导出与备份 | 完成（CSV/HTML、在线备份、恢复回滚） | 38 项 | **PG 备份未验** |
| 前端八页 | 完成（真实接口、登录、任务中心） | 85 项 + typecheck/build | **未做浏览器点击级 E2E** |
| 部署 | CLI + 脚本 + Compose + 文档 | CLI 30 项、脚本语法检查 | **未构建容器** |
| 运维接口 | 任务视图/列表/取消/暂停/恢复/事件、候选列表、最新开奖、倒序分页、日期筛选、来源观察明细、提供商状态、备份列表、结算重算 | 27 项集成测试 | 本机 |

## 三、未验证项与所需输入

| 项 | 现状 | 需要 |
|---|---|---|
| 真实数据源 | 只有合成录制源（`synthetic=true`、`license.verified=false`） | 供应商、凭证、保存/展示许可 |
| 真实 AI 服务 | 只有 FakeProvider 与 MockTransport | OpenAI 兼容的 `base_url`/`model`/`api_key` |
| PostgreSQL | 迁移与仓储测试只在 SQLite | 一个可用 PG 实例（或 Docker） |
| Docker/Compose | Dockerfile 与 compose 未构建 | Docker Desktop 或目标 Linux 主机 |
| 浏览器 E2E | 未做点击级验证 | 可启动的无头浏览器环境 |

## 四、已知功能缺口（不影响主闭环）

1. 队列与并发：等待限额（全局 50、每用户 3）与按类型并发上限（每来源 1 个采集、1 个回测、1 个 AI）
   已实现；worker 领取时按运行中数量跳过，限额释放后可继续领取。
2. 来源与 AI 的配置写入：AI 凭证已支持在软件内设置（`PUT /DELETE /admin/providers/ai`，AES-256-GCM
   加密落库，worker 启动时环境变量优先、数据库回退）；仍无 `POST /admin/providers/{id}/test` 连接测试。
3. 结算已提供服务层入口与管理员触发接口（`POST /admin/settlements/reconcile`，幂等）；
   仍缺自动触发（开奖修订发布后由采集流程调用）。
4. 开奖数据与回测结果导出（CSV/JSON）均已实现并接入前端；仍缺日志与费用查询接口。
5. 多 worker 下候选去重与封存单开仅服务层保证，PG 需补唯一索引/行锁。
6. 前端 1440/1280 未做像素级截图验收。

## 五、复现步骤

```powershell
# 1) 后端
cd backend
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pytest

# 2) 前端
cd ..\web
node <node.exe> <pnpm.mjs> typecheck
node <node.exe> <pnpm.mjs> build
node <node.exe> <pnpm.mjs> test:unit

# 3) 一键门禁
cd ..
powershell -ExecutionPolicy Bypass -File tooling/check.ps1

# 4) 端到端烟测（另开终端启动 API 与 worker，worker 设 DAJIDALI_SOURCE=recorded）
backend\.venv\Scripts\python.exe tooling\smoke_local.py --base-url http://127.0.0.1:8000 --username admin --password ***
```

## 六、风险与限制

- 历史封存测试是“回溯样本外”，不是绝对盲测；200 条轨迹不是 200 倍独立开奖样本。
- 统计与候选的未来数据隔离靠“只读前缀 + 快照”，已用篡改未来数据的测试证明。
- 备份排除密钥与会话，但 `secret_reference` 保留非敏感引用行（设计 12.1 允许）。
- 维护模式是进程内的；多进程部署需要在反向代理/worker 层接入只读开关。
- 沙箱环境限制（临时目录、Vite 子进程、Git 推送凭据）只影响本机开发，见[环境验收记录](2026-11-02-g0-environment.md)。
