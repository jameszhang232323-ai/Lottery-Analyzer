# 运维手册

本文覆盖日常日志、进程、任务、备份、故障排查和密钥注意事项。设计依据见
B/S 详细设计 6.4、11.3、12.2—12.4。

> 结论状态：命令与路径按当前代码整理；**未在真实长期运行、PostgreSQL、HTTPS 或
> 恢复演练中验证**。文末“未验证项”必须随发布报告保留。

## 1. 进程与端口

| 进程 | 职责 | 本机默认 | 服务器（Compose） |
|---|---|---|---|
| API | FastAPI、会话、任务入队 | `127.0.0.1:8000` | `api:8000`，仅 proxy 可达 |
| worker | 单进程领取并执行任务 | 无端口 | `worker` |
| scheduler | 调度租约与按时槽入队 | 无端口 | `scheduler` |
| proxy | 同源静态资源与 `/api` 反代 | 本机不启用 | `proxy:8080`，唯一映射端口 |
| PostgreSQL | 服务器数据库 | 不适用（SQLite） | `postgres:5432`，不公开 |

约束：

- SQLite 本机模式只能运行**一个** worker；不要为了“加速”启动第二个。
- scheduler 独立运行；多个实例靠数据库租约互斥，但首版仍建议只运行一个。
- API 可以多副本的前提是已完成 PostgreSQL 并发验收；当前未验证。
- API 进程启动时绝不执行迁移；迁移统一走 `init-db`。

## 2. 日志

### 本机

启动脚本把日志写到数据目录：

```text
backend\data\logs\api-YYYYMMDD-HHMMSS.out.log
backend\data\logs\api-YYYYMMDD-HHMMSS.err.log
backend\data\logs\worker-*.out.log
backend\data\logs\scheduler-*.out.log
```

PowerShell 查看时显式指定 UTF-8，避免中文乱码：

```powershell
Get-Content -LiteralPath .\backend\data\logs\api-20261103-092000.err.log -Encoding UTF8 -Tail 100
```

### Docker

```bash
docker compose -f deploy/compose.yaml logs -f --tail=100 api
docker compose -f deploy/compose.yaml logs -f --tail=100 worker scheduler
docker compose -f deploy/compose.yaml logs -f --tail=100 proxy postgres
```

Compose 已给每个服务配置 `json-file` 轮转（10 MB × 5）。日志中不应出现密码、主密钥
或完整 Cookie；发现泄露立即按安全事件处理并更换凭证。

## 3. 健康检查

| 探针 | 用途 | 期望 |
|---|---|---|
| `/api/v1/health/live` | 进程可响应 | `{"status":"ok"}` |
| `/api/v1/health/ready` | 就绪 | `{"status":"ready","checks":{"api":"ok"}}` |
| nginx `/healthz` | proxy 自身 | `ok` |

本机：

```powershell
cd backend
.\.venv\Scripts\python.exe -m dajidali healthcheck `
  --url http://127.0.0.1:8000/api/v1/health/ready --timeout 10
```

容器：

```bash
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml exec api \
  python -m dajidali healthcheck --url http://127.0.0.1:8000/api/v1/health/ready --timeout 5
```

注意：当前 ready 探针只报告 API 自身，不代表数据库、worker 或调度器健康；真实
依赖检查待后续任务补齐。健康检查通过不等于业务可用。

## 4. 任务队列

任务状态保存在 `job`、`job_event` 表；租约字段包括 `lease_token`、
`lease_expires_at`。排查顺序：

1. 确认 worker 进程存活、日志无 `UNKNOWN_JOB_KIND`/`JOB_FAILED`；
2. 查看任务状态与错误：

```sql
-- SQLite（用 Python sqlite3 或 sqlite3 工具执行）
SELECT id, kind, status, attempt, created_at, lease_expires_at, error_code
FROM job ORDER BY id DESC LIMIT 20;

SELECT job_id, event_type, created_at
FROM job_event ORDER BY id DESC LIMIT 50;
```

```bash
# PostgreSQL
docker compose -f deploy/compose.yaml exec postgres \
  psql -U dajidali -d dajidali -c \
  "SELECT id, kind, status, attempt, lease_expires_at, error_code FROM job ORDER BY id DESC LIMIT 20;"
```

3. 过期租约由 worker 下一轮的 `recover_expired` 回收；不要手工改 `lease_token` 或
   把 `running` 直接改成 `succeeded`，否则可能产生重复有效结果。
4. 当前 worker 的业务处理器注册表可能为空（见“未验证项”）。遇到未知类型，任务会被
   标记失败而不是静默丢弃；修复注册后重新入队，不要直接改状态。
5. 调度去重依赖 `(schedule_id, scheduled_for)` 唯一键；重复调度返回原任务。
   排查时不要删除 `scheduler_lease` 行来“强制跑”，应等待租约过期或正常释放。

## 5. 备份

### 本机 SQLite

建议停写后备份，避免只复制主文件而漏掉 WAL：

```powershell
powershell -ExecutionPolicy Bypass -File tooling\stop_local.ps1
Copy-Item .\backend\data\dajidali.sqlite3 .\backups\dajidali-20261103.sqlite3
Copy-Item .\backend\data\dajidali.sqlite3-wal .\backups\ -ErrorAction SilentlyContinue
Copy-Item .\backend\data\dajidali.sqlite3-shm .\backups\ -ErrorAction SilentlyContinue
```

如安装了 SQLite 命令行工具，在线备份可用：

```powershell
sqlite3 .\backend\data\dajidali.sqlite3 ".backup '.\backups\dajidali-20261103.sqlite3'"
```

附件、导出文件和运行日志按需一并归档；备份中不应包含 `.env` 或主密钥。

恢复：

1. 停止 API/worker/scheduler；
2. 保留当前数据库副本；
3. 恢复数据库与附件；
4. 启动并先跑 `init-db`（确认结构版本）；
5. 检查登录、任务队列、锁定/结算数据；不匹配时回退当前副本。

### 服务器

见 [deployment.md](deployment.md) 的 PostgreSQL 与附件卷小节。维护窗口内停止写入，
记录备份时间点、镜像 digest 和数据库结构版本；恢复前先在隔离库试恢复。

## 6. 密钥与凭证

- `DAJIDALI_MASTER_KEY` 是 32 字节 base64，只由后端读取；不得写入数据库、日志、
  `.env.example`、镜像层、URL 或前端资源。
- 主密钥与数据库备份分开保管；丢失主密钥后，已保存的服务密钥无法解密。
- 当前版本**没有**自动密钥轮换：直接替换 `DAJIDALI_MASTER_KEY` 会让旧密文全部失效。
  轮换前需要规划“解密旧密文—用新密钥重新加密—更新 key_version”的工具和演练；
  在工具完成前不要在生产环境直接换键。
- 恢复备份后：撤销会话、重新配置来源/AI 凭证、检查主密钥版本，再恢复任务。
- `.env` 权限限制为部署用户可读；不要通过命令行参数传递主密钥。
- 管理员密码创建命令会把明文放进 shell 历史；生产环境执行后清理历史，后续版本
  应提供从受控输入读取密码的方式。

## 7. 故障排查

| 现象 | 排查 |
|---|---|
| 端口被占用 | `Get-NetTCPConnection -LocalPort 8000` 或 `ss -ltnp`；换端口或停占用进程 |
| 重复启动 | 先 `stop_local.ps1`；检查 `data/run/*.pid` 是否残留；不要手工删除仍在运行进程的 PID |
| 未找到 Python | 确认 `backend\.venv\Scripts\python.exe` 存在；重建虚拟环境并安装依赖 |
| 未找到前端 | `web\dist\index.html` 不存在；在 web 目录 `pnpm build`；后端仍可运行 |
| `init-db` 失败 | 看迁移输出；确认 `DATABASE_URL`、目录权限、PG 连通性；失败后不要反复启动 API |
| API 60 秒未就绪 | 看 api `.err.log`；确认端口、依赖、数据库路径；脚本会清理本次 PID |
| 登录后立刻失效 | `DAJIDALI_HTTPS=1` 却用 HTTP 访问，Secure Cookie 不会发送；本机设 0 |
| 数据库 locked | SQLite 写锁竞争；确认只运行一个 worker，缩短写事务，稍后重试 |
| 容器无法写 `/data` | 命名卷属主应为 10001；bind mount 需 `chown 10001:10001` |
| PG 连接失败 | 检查 `DATABASE_URL` 驱动/密码/主机；确认镜像内已安装 `psycopg` |
| 调度不执行 | 确认 scheduler 存活、租约未过期、业务 tick 已注册；看 `scheduler_lease` |
| 任务一直 queued | worker 是否存活、处理器是否注册、队列是否达到上限；看 `job_event` |
| 页面 404（本机） | 未设置 `DAJIDALI_STATIC_DIR` 或 dist 缺失；重新运行启动脚本或 `pnpm build` |
| 代理 502 | API 容器不健康或重启；看 `docker compose ps` 与 api 日志 |

## 8. 启停速查

本机：

```powershell
powershell -ExecutionPolicy Bypass -File tooling\start_local.ps1 -OpenBrowser
powershell -ExecutionPolicy Bypass -File tooling\stop_local.ps1
powershell -ExecutionPolicy Bypass -File tooling\verify.ps1
```

Compose：

```bash
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml restart api worker scheduler
docker compose -f deploy/compose.yaml down            # 保留命名卷
docker compose -f deploy/compose.yaml down -v         # 危险：删除数据库与附件卷
```

`down -v` 只能在确认已备份且不再需要数据时使用。

## 9. 未验证项

- 真实长期运行、日志轮转、进程崩溃后的自动重启与 PID 文件清理。
- 本机备份/恢复、SQLite 在线备份、附件归档与恢复演练。
- PostgreSQL 的 `pg_dump`/`pg_restore`、并发领取、行锁和真实任务处理器。
- HTTPS/Secure Cookie、外部反向代理、Host/Origin 校验与证书续期。
- 主密钥轮换工具（当前未实现）与恢复后重新配置凭证的完整流程。
- 备份 API、导出下载和恢复预览的真实权限/越权测试。
- 5 名在线用户、回测期间 CPU/内存/时延指标。

## 10. 相关文档

- [本机快速开始](quick-start.md)
- [服务器部署](deployment.md)
- [设计文档 11.3、12.1—12.4](../superpowers/specs/2026-11-02-dajidali-bs-design.md)
