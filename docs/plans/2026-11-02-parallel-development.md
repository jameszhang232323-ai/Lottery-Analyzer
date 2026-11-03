# 并行开发编排 · 2026-11-02

本文件记录当前开发波次、轨道划分、文件所有权与合并规则，并给出可以复制到**独立会话窗口**执行的任务说明。
依据：[B/S 版实施计划](2026-11-02-dajidali-bs-implementation-plan.md)（任务 00—12）。

## 一、当前状态

| 项 | 状态 |
|---|---|
| 工程骨架（后端 + 前端 + 依赖锁） | 完成，提交 `a6a626b` |
| 共享契约（号码、策略、规范化哈希） | 完成：`domain/draw.py`、`domain/strategy.py`、`domain/canonical.py`，49 项测试 |
| 任务 02 数据层（SQLite、快照、任务租约、迁移） | 完成，提交 `6f6df3f`，integration 23 项 |
| 任务 03 安全（登录、会话、CSRF、归属、密钥） | 完成，提交 `280a3d6`，security 34 项 |
| 任务 05/06 引擎（统计、随机流、S0/S1、条件） | 完成，提交 `9c19326`，unit 197 项 |
| 前端八页骨架（任务 01/11 的部分） | 完成，提交 `05198f8`，前端单测 50 项 |
| 任务 07 锁定/奖级/结算（含 0003 迁移） | 完成，提交 `84d8c45`，新增 48 项 |
| 任务 08 回测引擎（纯计算） | 完成，提交 `77e6da5`，新增 59 项 |
| 全量基线 | 后端 **532** 项（2 skip）+ 前端 **50** 项，`tooling/check.ps1` 全绿 |
| 任务 09 AI（0004 迁移、证据包、校验、额度） | 完成，新增 83 项 |
| 任务 10 导出/备份/恢复/两库核对 | 完成，新增 38 项（2 skip：PG、符号链接） |
| 任务 12 部署（CLI、脚本、Compose、文档） | 完成，CLI 30 项；容器与 PG 未验证 |
| 任务 04 采集（来源适配、录制源、同步续传、开奖 API） | 完成可先行部分，44 项；真实来源未验证 |
| 任务 05/06/08 服务与 API（含 0005 迁移） | 完成，29 项 |
| 快照接口（打通分析/候选/回测入口） | 完成，5 项 |
| 任务 11 前端联调（登录、任务中心、八页接真实接口） | 完成，前端 85 项 |
| 全量基线 | 后端 **614** 项（2 skip）+ 前端 **85** 项，`tooling/check.ps1` 全绿 |
| 端到端烟测 | 通过：采集 39 期 → 快照 39 成员 → 统计 840 事实 → 候选 5 组 |
| W6 待做 | 真实来源、真实 AI、PG、Docker Compose 完整验收（需要凭证与环境） |

## 二、依赖关系

```text
任务00 准入 ──┬─> 任务04 采集（需要真实来源）
              └─> 任务09 AI（需要真实模型）

契约(已完成) ─┬─> 任务01 骨架(已完成)
              ├─> 任务02 数据层 ──┬─> 任务03 安全 ──> 任务04
              │                   ├─> 任务07 锁定/奖级
              │                   └─> 任务10 导出/备份
              ├─> 任务05 统计 ──> 任务06 候选 ──┬─> 任务07
              │                                  └─> 任务08 回测 ──> 任务09
              └─> 任务11 八页联调 ──> 任务12 部署验收
```

## 三、波次安排

| 波次 | 轨道 | 任务 | 依赖 | 并行度 |
|---|---|---|---|---|
| W1（完成） | A 数据层 | 02 数据库、快照、任务底座（SQLite） | 契约 | 3 条并行 |
| W1（完成） | B 引擎 | 05 统计 + 06 随机流/候选 | 契约 | |
| W1（完成） | C 前端 | 01 界面 + 11 页面骨架 | 契约 | |
| W2（完成） | D 安全 | 03 登录、角色、归属、密钥 | A 完成 | 2 条并行 |
| W2（完成） | E 结算 | 07 锁定、奖级、修订结算 | A+B | |
| W3（完成） | F 回测 | 08 时间顺序回测、B0/B1、bootstrap | B | 2 条并行 |
| W4（完成） | G AI | 09 证据包、校验、限额、任务执行 | B+契约 | 3 条并行 |
| W4（完成） | H 导出 | 10 报告、备份、恢复、两库迁移 | A+E | |
| W4（完成） | I 部署 | 12 CLI、本机脚本、Compose、文档 | A | |
| W5（下一步） | J 联调 | 11 八页联调、任务中心、前端接真实接口 | C+D+E+F+G+H | 串行 |
| W6 | K 验收 | 真实来源、真实 AI、PG、Compose 完整验收 | 全部 + 凭证 | 串行 |

任务 00（数据源、AI 准入）与代码波次**互不阻塞**：它决定“在线交付门槛”，代码可以先在本地数据与合成样例上完成。

## 四、文件所有权（避免并行冲突）

| 轨道 | 独占目录/文件 |
|---|---|
| A 数据层 | `backend/src/dajidali/{data,jobs}/**`、`backend/migrations/**`、`backend/alembic.ini`、`backend/tests/integration/**` |
| B 引擎 | `backend/src/dajidali/engine/**`、`backend/tests/unit/test_{statistics,omission,rng,candidates,constraints}.py`、`fixtures/golden/**` |
| C 前端 | `web/src/**`、`web/tests/**`、`web/vitest.config.ts` |
| 共享（只读） | `backend/src/dajidali/domain/**`、`backend/pyproject.toml`、`web/package.json`、`.gitignore` |

规则：

1. 共享契约**只能由集成负责人在合并时修改**；轨道内发现契约不足，先报告，不要自行改。
2. 每个轨道只提交自己目录内的文件；提交前的验证命令写在任务说明里。
3. 集成由主会话执行：`ruff` → `pytest` → `typecheck` → `build` → 一次提交，避免交叉失败。
4. 同一时间最多 3 个轨道并行；新增轨道前先确认它不碰已占用目录。

## 五、可复制到独立会话的任务说明

> 每个会话只做一件事。开始前先 `git pull`，结束时报告改了哪些文件、跑了什么命令、结果如何；不要跨轨道改文件。

### 会话 1 · 任务 00 数据源准入（需要账号与许可）

```text
在 D:\codex project\福彩分析 里执行实施计划任务 00 的数据源部分（docs/superpowers/plans/2026-11-02-dajidali-bs-implementation-plan.md 任务 00）。

要求：
1. 对候选来源（官方开奖公告、聚合数据、接口盒子）分别测试：五年起点、中间年份、最近一期、跨年期号、连续区间、限流、错误鉴权、字段缺失。
2. 记录每个来源的：允许用途、保存/展示许可、调用额度、实测延迟、覆盖范围、修订能力；没有凭证就如实写 not_tested，不得用模拟结果替代。
3. 产出 docs/decisions/0002-source-qualification.md 与 fixtures/sources/manifest.json（只放脱敏样例与哈希，不放凭证）。
4. 不修改 backend/src 下任何代码；不提交任何真实密钥。
```

### 会话 2 · 任务 00 AI 准入（需要模型账号）

```text
在 D:\codex project\福彩分析 里执行实施计划任务 00 的 AI 部分。

要求：
1. 用一个可用模型做最小结构化响应测试：给定 AnalysisEvidenceV1 样例，要求返回 AiReportV1 结构（schema_version/request_id/snapshot_hash/observations/strategy_proposals/limitations）。
2. 记录：模型名、接口地址、超时、实际用量与费用口径、是否支持 JSON 模式、一次格式纠正是否有效。
3. 测三种失败：未知 fact_id、越界参数、超时；确认这些结果不会被误判为可用。
4. 产出 docs/decisions/0003-ai-qualification.md 与 fixtures/ai/（脱敏请求响应）。
5. 不修改 backend/src；不写凭证到仓库。
```

### 会话 3 · 任务 03 安全（W2，等 A 完成）

```text
在 D:\codex project\福彩分析 里执行实施计划任务 03：登录、角色、资源归属与密钥。

先读设计 6.4、11.2、13.1 与任务 03 说明。只改：
backend/src/dajidali/security/**、backend/src/dajidali/api/routes/{auth,admin}.py、backend/tests/security/**、web/src/views/LoginView.vue。

要求：
1. 密码哈希、会话令牌只存哈希、HttpOnly Cookie、CSRF 校验、Host/Origin 校验。
2. 逐对象授权（owner_id），不可见对象统一 404；管理员接口单独鉴权。
3. 服务密钥认证加密保存，读取只返回掩码；测试断言浏览器响应、日志、导出里都不出现密钥。
4. 先写测试：未登录、越权读他人任务、禁用账号、跨站写、缺 CSRF。
5. 验证：backend 下 `ruff check src tests` 与 `pytest tests/security -q` 全绿。
```

### 会话 4 · 任务 08 回测（W3，等 B+07）

```text
在 D:\codex project\福彩分析 里执行实施计划任务 08：时间顺序回测与随机对照。

先读设计第九章与任务 08 说明。只改 backend/src/dajidali/engine/backtest/**、backend/tests/unit/test_{no_future_leakage,baseline_parity,holdout,bootstrap}.py。

要求：
1. 60/20/20 划分、热身期、排除原因；t 期候选只能读 t 之前的数据（用“篡改未来开奖不影响 t 期结果”的测试证明）。
2. B0/B1 各 200 条轨迹，同 K、同可评价期集合、域隔离种子；主指标每组平均红球命中数。
3. 10 期移动块 bootstrap 2000 次重采样，输出 95% 区间；样本 <30 期不给稳定性结论。
4. 冻结与封存：最多 12 套探索、验证后冻结、封存只开一次；全量尝试进注册表。
5. 验证：`pytest tests/unit -q` 与 `ruff check src tests` 全绿。
```

## 六、集成验收（主会话）

每波次结束后执行：

```powershell
cd backend
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pytest
cd ..\web
node <node> <pnpm> typecheck
node <node> <pnpm> build
node <node> <pnpm> test:unit
```

全部通过后统一提交；任一失败回到对应轨道，不跨轨道打补丁。
