# 大吉大利 Python与浏览器B/S版实施计划

文档版本：V1.1 B/S架构修订稿  
修订日期：2026年11月2日  
设计依据：[B/S版详细设计](../specs/2026-11-02-dajidali-bs-design.md)  
状态：设计与计划交付，尚未创建应用工程、部署服务或调用外部付费接口。

## 一、目标与技术基线

交付“大吉大利”Python后端和浏览器界面，支持本机运行与服务器B/S部署。功能为双色球五年数据、在线更新与更正、统计图表、研究候选、随机对照回测、AI证据分析、候选锁定及开奖核对、导出备份。

采用FastAPI＋Pydantic＋SQLAlchemy/Alembic，独立Python统计核心；Vue 3＋TypeScript＋Vite＋ECharts前端。SQLite服务本机模式，PostgreSQL服务服务器模式。后端API、worker和scheduler分别运行；持久化队列首版保存在数据库，关闭浏览器不停止任务。

源代码和依赖精确版本在任务00验证后锁定。运行端交付已构建静态资源，不要求用户安装Node.js；前端开发及构建需要Node.js。首版是单组织小团队应用，有管理员/分析员角色及个人实验归属，不建设多租户计费。

本机模式默认只监听127.0.0.1，启动Python服务后打开浏览器；服务器模式提供反向代理、HTTPS、登录、PostgreSQL持久化和部署文件。统计、回测、AI请求和密钥均在后端，浏览器不复制主计算和外部服务凭证。

## 二、周期、资源和阶段出口

以1名熟悉Python及Web的开发全职投入估算：开发 **46人日**，测试12—16人日，产品/设计4—6人日，合计62—68人日。加10%—15%缓冲，建议 **10—11周**。测试并行参与，不将开发与测试人日全部串行相加。实际周期受来源许可、凭证、服务器和反馈影响。

本次估算取代原桌面版43人日/9—10周基线，增加了登录权限、两类数据库、持久化任务及服务器交付。这里是工程估算，不是供应商正式报价。

| 阶段 | 建议时间 | 出口 |
|---|---|---|
| G0 准入 | 第1周 | Python/Web环境、真实来源与AI、两库和进程验证报告 |
| G1 基础服务 | 第2—4周 | 领域、API、数据库、登录授权、采集与开奖页 |
| G2 分析闭环 | 第4—7周 | 统计、候选、锁定核对、回测和随机对照 |
| G3 AI与使用闭环 | 第7—9周 | 真实AI、备份导出、八页完整操作流程 |
| G4 部署验收 | 第10周 | 本机发行包、服务器Compose、真实PG及浏览器验收 |
| 缓冲 | 第11周 | 缺陷、来源变动、恢复演练和验收遗留 |

阶段门槛通过需有证据，不只是模块声明。来源许可不足或真实服务未验证时，如实保留未通过项；可独立进行的本地数据与算法工作继续。用户当前只要求修订方案，实际开发待明确启动指令。

## 三、文件结构与接口边界

```text
backend/
  pyproject.toml                  Python依赖与工具配置
  src/dajidali/
    api/                         FastAPI路由、Schema与错误响应
    domain/                      彩种、规则、号码、策略和快照
    engine/                      统计、随机、候选、回测和奖级
    data/                        ORM、仓储、来源适配和迁移辅助
    services/                    同步、AI、锁定、核对、导出用例
    jobs/                        任务领取、租约、检查点、worker和scheduler
    security/                    账号、会话、归属授权和密钥
    cli.py                       初始化、管理和本机启动入口
  migrations/                    Alembic迁移
  tests/                         单元、契约、集成与安全测试
web/
  src/api/                       OpenAPI生成类型和HTTP客户端
  src/views/                     八个业务页面
  src/components/                图表、状态、参数、进度与事实引用
  src/stores/                    会话和页面状态，禁止保存服务密钥
  tests/                         前端单元与组件测试
  e2e/                           Playwright浏览器旅程
contracts/v1/                    Schema、请求响应与兼容样例
fixtures/                        合成小样本、边界、脱敏来源和AI样例
deploy/
  Dockerfile                     后端和预构建静态资源
  compose.yaml                   代理、API、worker、scheduler、PG
  nginx.conf                     同源入口和上传限制
  env.example                    不含凭证的配置模板
tooling/                         本机启动停止、检查、发布与数据库迁移验证
docs/acceptance/                  环境、来源、AI、两库、性能及部署证据
docs/user-guide/                  使用、配置、运维与恢复
```

domain不导入Web/ORM；engine不自行联网或读数据库。services执行用例与授权，worker调用同一服务入口。网页通过/api/v1交互，OpenAPI生成前端类型；不能以浏览器状态为后台任务或锁定状态的事实来源。

## 四、任务分解

任务先固定契约和失败样例，再写能暴露问题的测试、实现、运行对应检查、保存证据。每任务交付为可评审的功能切片；测试数据不覆盖正式用户数据。只有文档变化时不运行应用测试冒充产品验收。

### 任务00 来源、模型与B/S环境准入

**工期：3人日；前置：本设计基线。**

文件：`docs/decisions/0001-bs-stack.md`、`0002-source-qualification.md`、`0003-ai-qualification.md`；`fixtures/sources/manifest.json`；`docs/acceptance/g0-feasibility.md`。

产出：支持的Python/Node版本、依赖锁定策略、来源能力与许可、AI模型连接状态、目标部署环境。来源能力包括历史查询方式、最早已验证日期、限流、修订能力、保存/展示许可。

步骤：

1. 检查工作区、Python虚拟环境、Node构建工具、Windows及目标Linux运行条件，不覆盖其他项目环境。
2. 对真实来源测试五年起点、中间、最近年份、跨年及连续区间；验证官方规则、休市和期号目录，保留脱敏样例与哈希。
3. 核实长期保存与团队展示许可，估算初始化及复核调用量；无凭证则保持未验证，不用模拟通过替代。
4. 测试一个AI模型的最小结构化响应及用量信息；无可用账号时保留待验证状态。
5. 用隔离验证代码证明SQLite/PG基本事务、Python子进程spawn、API/worker分离和前端构建可用；临时验证不并入正式产品。

验收：来源只有最近100期时五年准入失败；AI没有凭证时live_ai_status为not_tested；Windows上的计算子进程结束后无残留。在线来源未准入，不承诺五年在线交付。

### 任务01 Python工程、领域契约与Web骨架

**工期：3人日；前置：任务00技术路线确认。**

创建：`backend/pyproject.toml`、`backend/src/dajidali/api/app.py`、`api/errors.py`、`domain/draw.py`、`domain/rules.py`、`domain/snapshot.py`、`domain/strategy.py`；`web/package.json`、`web/src/router.ts`、`web/src/App.vue`；`contracts/v1/draw-revision.schema.json`。

测试：`backend/tests/unit/test_domain.py`、`test_rule_versions.py`；`web/tests/app-shell.spec.ts`。

消费：来源和规则准入结论。产出：DrawRevisionV1、DatasetSnapshotV1、StrategyConfigV1、统一错误结构及八页路由。

步骤：

1. 先写号码范围、重复红球、3D前导零、期号排序与规则边界测试。
2. 实现不可变领域对象和明确错误码；API输入输出使用Pydantic，拒绝未定义的敏感字段。
3. 建立前后端工程、锁文件、静态资源服务和/api/v1前缀，公开最小存活探针。
4. 生成OpenAPI和TypeScript类型，八页显示真实空状态，不以示例结果冒充实际数据。

验收：红球[1,1,3,4,5,6]被拒绝；3D的007序列化后仍为007；非法请求返回422及request_id。纯引擎模块不得导入FastAPI或SQLAlchemy。

### 任务02 数据库、快照与持久化任务底座

**工期：4人日；前置：任务01。**

创建：`backend/src/dajidali/data/models.py`、`data/database.py`、`data/repositories/draws.py`、`snapshots.py`；`jobs/repository.py`、`jobs/worker.py`、`jobs/scheduler.py`；`backend/migrations/`。

测试：`backend/tests/integration/test_revisions.py`、`test_snapshots.py`、`test_job_leases.py`、`test_scheduler_uniqueness.py`、`test_migrations.py`。

产出：设计第六章各表，含user/session、owner_id、job/lease/event；仓储快照、事务领取、幂等提交和迁移入口。

步骤：

1. 先写重复导入、修订更正不改变旧快照、事务中断、双领取及失效租约提交测试。
2. 实现原始观察、当前修订指针、证据、快照成员及唯一约束，金额NULL语义完整保留。
3. SQLite实现短事务领取且限定单worker；PG实现行锁领取。租约token、心跳、过期恢复与检查点提交原子化。
4. scheduler使用独立租约和schedule_id＋scheduled_for唯一键，API进程不启动调度。
5. 在SQLite与真实PG运行相同仓储契约和迁移测试；计算子进程不传数据库连接。

验收：旧快照仍引用revision=1，新快照引用2；两个worker竞争时只有一个有效提交；租约过期后旧token提交被拒绝；两个scheduler不重复入队。实现至少一次执行＋幂等结果，不声称执行恰好一次。

### 任务03 登录、角色、资源归属和密钥

**工期：4人日；前置：任务02。**

创建：`backend/src/dajidali/security/passwords.py`、`sessions.py`、`authorization.py`、`secrets.py`；`api/routes/auth.py`、`admin.py`；`web/src/views/LoginView.vue`、`web/src/api/client.ts`。

测试：`backend/tests/security/test_auth.py`、`test_csrf.py`、`test_object_ownership.py`、`test_secret_redaction.py`。

接口：POST /auth/login、/auth/logout，GET /auth/me；管理员连接设置；服务层require_owner及require_admin检查。

步骤：

1. 先写未登录、分析员改全局配置、A用户读B任务/文件/候选、禁用会话和跨站提交测试。
2. 实现管理员初始化命令、密码哈希、会话令牌哈希、HttpOnly Cookie及CSRF验证，按部署模式设置Secure/SameSite。
3. 所有个人对象和下载在服务端逐项授权；公共开奖与个人实验分开，支持访问保留审计。
4. 服务密钥认证加密，主密钥由部署secret或受控环境注入。写入接口不回显，读取只返回掩码；前端资源和localStorage不含密钥。
5. 校验Host/Origin，关闭默认跨域；可配置来源/模型地址仅管理员可改，检查目标及重定向。

验收：跨用户读取和操作全部拒绝；浏览器网络响应无服务密钥；缺CSRF的写请求拒绝；本机loopback也不能接受任意网页写入。安全测试不能仅检查菜单是否隐藏。

### 任务04 在线采集、覆盖与开奖数据页

**工期：4人日；前置：任务02、03及真实来源准入。**

创建：`backend/src/dajidali/data/sources/base.py`、`qualified_provider.py`；`services/sync.py`、`coverage.py`、`reconciliation.py`；`api/routes/draws.py`、`sync_jobs.py`；`web/src/views/DrawsView.vue`。

测试：`backend/tests/contracts/test_source.py`、`integration/test_sync_resume.py`、`test_coverage.py`；`web/tests/draws.spec.ts`。

接口：GET /draws、GET /calendar、POST /sync-jobs；任务返回202和job_id；来源只由后端访问。

步骤：

1. 用脱敏录制验证HTTP200业务错误、403、429、超时、错误JSON、缺奖金及字段变化。
2. 实现五年分页/日期初始化、限流、退避及事务后推进检查点；补漏使用已核验期号清单。
3. 实现最近30期复核和历史滚动复核，即使最新期号不变也能发现旧期更正。
4. 展示期号、来源、核验级别、完整性、修订和任务进度。管理员接受修订，分析员读取公共结果。
5. 实测五年范围和停机恢复，保存覆盖报告。缺期、冲突、休市分别说明，不能用样例填充真实缺口。

验收：第2页未提交时中断，重启从第2页恢复；关闭浏览器任务继续；新网页可查同一任务；旧期更正产生修订和失效事件。来源权限与数据质量未通过项保持可见。

### 任务05 统计引擎和分析图表

**工期：3人日；前置：任务04。**

创建：`backend/src/dajidali/engine/statistics/frequency.py`、`omission.py`、`structure.py`、`analysis.py`；`services/analysis.py`、`api/routes/analysis_jobs.py`；`web/src/views/AnalysisView.vue`、`components/MetricChart.vue`。

测试：`backend/tests/unit/test_statistics.py`、`test_omission_boundaries.py`；`web/tests/analysis.spec.ts`。

产出：事实集、fact_id、单位、样本、公式版本和快照；POST /analysis-jobs及结果读取。

步骤：人工构造10/30期并手算；先固定期望值；实现设计第七章公式、截断遗漏和不足窗口；worker执行，前端仅渲染；比较图表、明细和后端事实；记录目标环境耗时。

验收：红球出现期频率总和为6、蓝球为1；30期窗口从未出现显示“至少30期”；改变筛选后旧任务结果不能覆盖新页面。

### 任务06 复现随机流与候选策略

**工期：3人日；前置：任务05。**

创建：`backend/src/dajidali/engine/random_stream.py`、`strategies/uniform.py`、`frequency_mixture.py`、`constraints.py`、`candidates.py`；`services/candidates.py`、`api/routes/candidate_jobs.py`；`web/src/views/StrategiesView.vue`。

测试：`backend/tests/unit/test_rng.py`、`test_candidates.py`、`test_constraints.py`；`fixtures/golden/rng-v1.json`、`candidates-v1.json`。

产出：设计附录的sha256-counter-v1、S0/S1、参数白名单、候选及尝试计数。

步骤：先验证随机流编码和无偏取模；实现均匀无放回与批内去重；实现平滑频率及剩余号码权重重算；加入结构检查和100,000次上限；连接后端任务和参数预览。

验收：lambda=0得到均匀权重；固定号码与排除冲突立即报错；相同快照/版本/种子/参数输出完全相同；Windows/Linux结果与黄金样例一致。候选权重不显示为中奖概率。

### 任务07 锁定、奖级和修订结算

**工期：3人日；前置：任务06、数据修订和权限服务。**

创建：`backend/src/dajidali/engine/settlement.py`、`rule_resolver.py`；`services/candidate_ledger.py`、`settlement.py`；`api/routes/candidates.py`；`web/src/views/CandidateRecordsView.vue`。

测试：`backend/tests/unit/test_prize_rules.py`；`integration/test_locking.py`、`test_settlement_revisions.py`、`test_lock_concurrency.py`。

产出：POST /candidates/{id}/lock及核对读取；后端时间和事务锁定；奖级匹配器供回测复用。

步骤：先写所有拒绝条件；在事务内检查owner、expected_revision、截止证据、服务时间和目标数据；保存不可改写核心和哈希；按期号解析新旧规则、特别规定及福运奖；幂等结算、更正追加、金额未知保留。

验收：客户端修改时间不能绕过截止；两个过期版本并发只接受合法版本；已锁定核心不可修改；3红无蓝且特别规定启用匹配福运奖，模式未知时金额不猜测；更正后旧结算仍可查。

### 任务08 时间顺序回测和随机对照

**工期：5人日；前置：任务06、07。**

创建：`backend/src/dajidali/engine/backtest/plan.py`、`walk_forward.py`、`baselines.py`、`metrics.py`、`bootstrap.py`；`services/backtests.py`、`api/routes/backtest_jobs.py`；`web/src/views/BacktestsView.vue`。

测试：`backend/tests/unit/test_no_future_leakage.py`、`test_baseline_parity.py`、`test_holdout.py`、`test_bootstrap.py`；`integration/test_backtest_resume.py`。

产出：冻结计划、逐期结果、B0/B1、主次指标、区间、全尝试注册及取消恢复。

步骤：

1. 先做未来数据变形测试；t期候选只能读取t之前信息。
2. 实现60/20/20、热身和排除说明、最多12套探索、冻结和封存只开一次。
3. 实现B0/B1各200条轨迹、同K同可评价集合和域隔离种子。
4. 实现按期差值、长度10的移动块bootstrap及2,000次重采样；不把组数/轨迹当成独立开奖次数。
5. worker分批计算和保存检查点，验证取消、过期租约和恢复；同时测查询API，避免CPU占满导致页面不可用。

验收：只修改t及未来结果不改变t期候选；B1使用相同约束；读取封存后调参只能标探索；中断恢复与未中断结果一致；没有优势也能生成合格报告。

### 任务09 AI证据、连接和受限建议

**工期：4人日；前置：任务05、08、真实AI账号。**

创建：`backend/src/dajidali/services/ai/provider.py`、`qualified_provider.py`、`evidence.py`、`validator.py`、`usage_limiter.py`；`api/routes/ai_jobs.py`；`web/src/views/AiView.vue`、`components/EvidencePreview.vue`、`ProposalDiff.vue`。

测试：`backend/tests/unit/test_ai_evidence.py`、`test_ai_validation.py`、`test_usage_limits.py`；`integration/test_ai_unknown_outcome.py`；`fixtures/ai/`。

产出：AnalysisEvidenceV1、AiReportV1、一个真实Provider、每用户和全局次数/费用限制、原响应引用。

步骤：先覆盖未知fact_id、伪造数字、错误快照、越界参数和代码/URL字段；资料预览后后端调用；本地事实模板填数字；建议预览后提交受限引擎；最多一次主调用＋一次格式纠正；超时或丢响应标未知，不自动重复收费调用。

验收：不合格响应不可应用；事实引用、窗口和单位一致；浏览器不出现密钥；AI失败不影响后端统计；权限用户不能读他人AI记录；真实连接与模拟测试证据分开保存。

### 任务10 报告下载、备份和两库迁移

**工期：3人日；前置：任务02及完整结果。**

创建：`backend/src/dajidali/services/exports.py`、`artifacts.py`、`backup.py`、`restore.py`、`database_transfer.py`；`api/routes/artifacts.py`、`backups.py`；`web/src/views/BackupView.vue`。

测试：`backend/tests/integration/test_backup_restore.py`、`test_sqlite_pg_transfer.py`；`security/test_artifact_access.py`、`test_archive_paths.py`；`unit/test_export_escaping.py`。

产出：CSV/JSON/HTML、鉴权下载、备份清单、隔离恢复和SQLite→PG核对报告。

步骤：先写WAL备份、哈希损坏、路径穿越、跨版本、磁盘不足和密钥排除测试；实现后端文件ID与归属；管理员维护窗口协调DB/附件快照；隔离恢复预览后切换，撤销会话重配密钥；两库迁移核对行数、快照和候选/结算哈希。

验收：用户A不能下载B文件；备份无有效会话和密钥；坏包不损坏当前库；恢复后锁定及结算一致；SQLite成功不替代PG恢复验收。

### 任务11 八页联调与浏览器使用闭环

**工期：3人日；前置：任务04—10。**

创建/完善：`web/src/views/OverviewView.vue`、`SettingsView.vue`；`components/TaskCenter.vue`、`DataStatus.vue`；`web/e2e/user-journey.spec.ts`、`browser-close.spec.ts`、`stale-result.spec.ts`；`docs/user-guide/quick-start.md`。

产出：八个业务页、登录页、全局状态、任务中心、错误反馈和用户指南。

步骤：统一快照/任务/全局数据状态；贯通采集→统计→AI→候选→回测→锁定→核对；检查1280×720、1440×900、125%/150%缩放；静态资源无CDN；处理无数据、失联、无解、AI失败、旧报告和待重算。

验收：关闭并重新打开浏览器可查看原任务真实状态；切换用户不会残留他人页面数据；快照A的迟到响应不覆盖B页面；断外网且本机服务运行仍可统计，断开后端连接则如实显示不可提交。

### 任务12 本机运行、服务器部署和交付验收

**工期：4人日；前置：任务11及真实服务证据。**

创建：`tooling/start_local.ps1`、`stop_local.ps1`、`verify.ps1`；`deploy/Dockerfile`、`compose.yaml`、`nginx.conf`、`env.example`；`docs/user-guide/deployment.md`、`operations.md`；`docs/acceptance/release-report.md`、`performance-baseline.md`。

产出：Python发行包、预构建前端、依赖和镜像校验、部署配置、升级回退、运维与验收报告。无Windows客户端安装包交付项。

步骤：

1. 验证普通用户环境本机启动/停止、端口冲突、重复启动、中文路径和隐藏辅助进程，运行端无需Node.js。
2. 在目标Linux以Compose部署反代/API/worker/scheduler/PG；HTTPS、Cookie、健康检查、卷和日志轮转通过，数据库不公开。
3. 浏览器走真实来源、真实AI及完整用户旅程；提前到来的模拟开奖只验证机制，长期真实前瞻成绩单独积累。
4. 重启API/worker/scheduler、模拟租约过期及取消，验证无重复有效结果；升级单次迁移及备份回退演练。
5. 5用户场景测读API P95、回测期间交互、CPU/内存、任务总时长和取消；保存版本、硬件、快照、参数及原始日志。

验收：两种部署和两种数据库各有真实报告；发布资源不含凭证；重建容器后数据完整；严重缺陷为0，未过项明确列出。仅开发机运行、模拟服务成功、界面截图均不能替代完整部署验收。

## 五、测试命令与证据

工程建立后，在对应目录使用锁定环境执行：

```powershell
# backend目录，项目虚拟环境已激活
python -m ruff check src tests
python -m pytest tests/unit tests/contracts tests/security
python -m pytest tests/integration
python -m alembic upgrade head
```

```powershell
# web目录，发布构建使用锁文件
npm ci
npm run typecheck
npm run test:unit
npm run build
npm run test:e2e
```

脚本逐项检查退出码，依赖顺序为数据库→一次迁移→API/worker/scheduler→浏览器测试；测试运行配置写入pyproject/package.json。集成测试分别指向隔离SQLite和PG，真实源/模型测试用显式开关和测试额度；默认离线测试不产生外部费用。上述是未来验收命令，本轮文档修订未执行这些产品测试。

归档证据：环境和锁文件、来源许可与覆盖、脱敏请求、AI校验、快照、任务恢复、两库契约、越权检查、性能原始数据、浏览器旅程、部署/升级/恢复报告及发行校验值。测试库、正式数据和缓存独立管理，不自动清理用户数据目录。

## 六、验收矩阵和完成定义

| 需求 | 任务 | 必备证据 |
|---|---|---|
| R01 Python＋浏览器B/S | 00/01/12 | 本机启动、服务器部署、浏览器和两库结果 |
| R02 五年数据 | 00/02/04 | 实际覆盖、预期日历、休市及缺口解释 |
| R03 更新更正与持续任务 | 02/04/12 | 浏览器关闭、检查点恢复、历史更正、scheduler去重 |
| R04 数学统计 | 05 | 独立人工样例、公式版本及图表对照 |
| R05 AI分析 | 03/09 | 真实调用、事实校验、权限及费用/未知状态 |
| R06 候选 | 06 | 合法性、去重、约束边界和黄金样例 |
| R07 回测与前瞻 | 07/08 | 防泄漏、B0/B1、冻结、截止和修订结算 |
| R08 权限与恢复 | 02/03/10/12 | 越权、密钥、两库、备份恢复和升级演练 |

模块完成：契约、测试、错误反馈及文档齐全。阶段完成：对应模块集成并跑通用户流程。产品完成：真实来源/AI、目标部署、数据恢复和交付资料完成。软件正确性合格不要求策略战胜随机；没有显示优势是可交付研究结果。

## 七、成本、风险和变更

工程费用按46开发人日＋12—16测试人日＋4—6产品设计人日乘实际报价，再计风险准备。现金成本包括数据源、AI及服务器/域名/证书/备份空间，不能把个人本机无主机费用套到服务器运行。

接口预算分初始化、开奖检查、最近30期复核和历史遍历；AI按真实用量和所选模型费率估算，价格未知时保留未知并以次数约束。浏览器刷新不创建新收费调用，重试幂等和未知结果处理列入验收。

| 风险 | 处置 |
|---|---|
| 五年来源或许可不足 | 更换有许可来源，在线门槛保持未通过 |
| 真实AI不可用 | Python分析继续，AI模块不标完成 |
| SQLite并发不足 | 服务器采用PG，实际迁移与回归后切换 |
| 租约超时导致重复执行 | token约束、幂等提交、检查点和故障测试 |
| 服务密钥泄漏或越权 | 后端保管、逐对象授权、网络和导出检查 |
| 回测没有优势 | 正常展示，不通过反复调参制造提升 |
| 目标部署性能不达标 | 用完整流程基线定位和复测，不以组件耗时替代 |

新增彩种、复杂训练、多租户计费、公开数据分发、高于5用户的并发目标、原生App和第三方时间认证另行评估。来源适配和本设计内缺陷修复按现有范围处理。

## 八、交付清单及后续顺序

首版交付：Python源代码和锁定环境、Web源码与预构建资源、API/OpenAPI、数据库模型与迁移、任务worker/scheduler、来源准入与覆盖、统计候选回测、真实AI协议、锁定核对、账号权限、导出备份、本机脚本、服务器Compose、升级回退、用户/运维指南和验收报告。

后续：福彩3D约1—2周；移动浏览器细化约1—2周；更高并发及worker扩展在测量后估算；PWA和原生App另行确定需求。持续采集和服务器部署已在首版范围内，不再作为未来独立建设前提。
