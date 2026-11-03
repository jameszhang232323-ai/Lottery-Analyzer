# 服务器部署

本文说明 Linux 服务器上的 Docker Compose 部署：`proxy`（非 root nginx）、`api`、
`worker`、`scheduler`、`postgres` 五个服务。`api`/`worker`/`scheduler` 共享同一
应用镜像、只覆盖启动命令；`proxy` 与 `postgres` 使用官方镜像。只有 proxy 向宿主机
暴露端口，数据库和 API 不公开。设计依据见 B/S 详细设计 12.3、12.4。

> 结论状态：Compose 文件已做 YAML 解析，Dockerfile/nginx 配置只做了静态检查；
> **当前环境没有 Docker，未执行镜像构建、容器启动、PostgreSQL 连接、HTTPS 或恢复演练**。
> 文末“未验证项”必须如实保留，不能把本文当作服务器验收证据。

## 1. 前置条件

- Linux x86_64/arm64，Docker Engine 24+ 与 Docker Compose v2。
- 可访问基础镜像仓库（node、python、nginx、postgres）；目标环境若离线，需要先导入镜像。
- 一个域名或受控内网地址、TLS 证书（启用 HTTPS 时）。
- 服务器时间同步；数据库与附件使用持久化卷。

## 2. 首次部署

在项目根目录执行：

```bash
cp deploy/env.example deploy/.env
# 编辑 deploy/.env，至少替换 POSTGRES_PASSWORD、DATABASE_URL 中的密码和 DAJIDALI_MASTER_KEY
```

生成主密钥（不要把输出写进 shell 历史或提交到仓库）：

```bash
python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
```

填入 `deploy/.env` 的 `DAJIDALI_MASTER_KEY`。`DATABASE_URL` 中的密码必须与
`POSTGRES_PASSWORD` 一致；密码含 `@`、`:`、`/` 等字符时需 URL 编码。

构建并启动：

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env build
docker compose -f deploy/compose.yaml --env-file deploy/.env run --rm api \
  python -m dajidali init-db
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d
```

说明：

- `init-db` 是唯一执行迁移的入口；API/worker/scheduler 启动时**不会**自动迁移。
  升级时同样先执行一次，再启动新版本。
- 服务器模式由 proxy/nginx 提供 `web/dist` 静态资源；**不要**给 api 服务设置
  `DAJIDALI_STATIC_DIR`（该变量只用于本机 API 直接托管前端）。
- `api` 的镜像 `HEALTHCHECK` 请求 `/api/v1/health/live`；proxy 等 API 健康后才启动。
- `worker` 与 `scheduler` 关闭了容器健康检查（它们不提供 HTTP）；进程退出由
  `restart: unless-stopped` 拉起。真实任务健康度要看日志与数据库队列。
- 首次启动后检查：

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/api/v1/health/ready
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail=100 api worker scheduler
```

`DAJIDALI_HTTP_PORT` 默认 8080；只有 proxy 使用该宿主机端口。

## 3. 管理员与密钥

首次部署后创建管理员：

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env run --rm api \
  python -m dajidali create-admin --username admin --password '至少8位的强密码'
```

- 密码会出现在命令历史和 Docker 审计中；生产环境建议在受控终端执行，事后清理历史。
- 主密钥只从 `DAJIDALI_MASTER_KEY` 读取，不写入数据库、镜像或镜像层。
- 更换主密钥前必须确认已有服务密钥可被重新加密；当前版本没有自动轮换命令，
  直接换键会导致已保存凭证无法解密。操作注意见 [operations.md](operations.md)。

## 4. HTTPS

当前 `deploy/nginx.conf` 只监听容器内 `8080`（HTTP），推荐由外层负载均衡/反向代理
终止 TLS，再转发到宿主机 `8080`。

### 方案 A：外层 TLS 终止（推荐）

1. 在负载均衡/外层 nginx 配置证书，`https://域名/` 转发到 `http://<服务器>:8080`。
2. 保留 `Host`、`X-Forwarded-For`、`X-Forwarded-Proto` 头。
3. 将 `deploy/.env` 的 `DAJIDALI_HTTPS` 设为 `1`，重建/重启 api（Secure Cookie 需要 HTTPS）。
4. 外层负责 80→443 跳转、HSTS 与证书续期。

> 当前后端尚未实现完整的 Host/Origin 白名单校验（设计 13.1 要求），
> 外层必须限制可访问域名/来源；此项列为风险与未验证项。

### 方案 B：在 Compose 内终止 TLS（未验证）

需要给 proxy 额外映射 443、挂载证书，并在 `deploy/nginx.conf` 增加 HTTPS server 块。
示意（不要直接照抄上线，需补齐证书路径、域名、协议版本和权限）：

```nginx
server {
    listen 8443 ssl;
    server_name example.com;
    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    location /api/ {
        proxy_pass http://dajidali_api;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }
}
```

同时修改 compose 的 proxy `ports`（例如 `443:8443`）和证书只读挂载。
`nginx-unprivileged` 容器不能监听 80/443，必须使用高位端口再映射。

## 5. 卷与数据

| 卷 | 用途 | 说明 |
|---|---|---|
| `dajidali_dajidali_pgdata` | PostgreSQL 数据目录 | 不要与其他项目共用 |
| `dajidali_dajidali_data` | `/data`：附件、导出、运行辅助文件 | API/worker 共享 |

卷名以 `docker volume ls` 实际输出为准（Compose 项目名固定为 `dajidali`）。
确认：

```bash
docker volume ls | grep dajidali
docker compose -f deploy/compose.yaml exec postgres df -h /var/lib/postgresql/data
```

附件卷由非 root 用户 `10001` 写入。使用命名卷时，首次创建会继承镜像 `/data` 的属主；
如果改成宿主机 bind mount，必须预先 `chown 10001:10001`，否则上传/导出会失败。

## 6. 备份与恢复

### 6.1 PostgreSQL

维护窗口内先停止写入：

```bash
docker compose -f deploy/compose.yaml stop worker scheduler api
docker compose -f deploy/compose.yaml exec -T postgres \
  pg_dump -U dajidali -d dajidali -Fc -f /tmp/dajidali.dump
docker cp "$(docker compose -f deploy/compose.yaml ps -q postgres):/tmp/dajidali.dump" \
  ./backups/dajidali.dump
docker compose -f deploy/compose.yaml start api worker scheduler
```

恢复（先在隔离环境验证 dump 可读）：

```bash
docker compose -f deploy/compose.yaml stop worker scheduler api
docker cp ./backups/dajidali.dump \
  "$(docker compose -f deploy/compose.yaml ps -q postgres):/tmp/dajidali.dump"
docker compose -f deploy/compose.yaml exec -T postgres \
  pg_restore -U dajidali -d dajidali --clean --if-exists /tmp/dajidali.dump
docker compose -f deploy/compose.yaml start api worker scheduler
```

### 6.2 附件卷

```bash
docker run --rm \
  -v dajidali_dajidali_data:/data:ro \
  -v "$PWD/backups:/backup" \
  alpine tar czf /backup/attachments.tgz -C /data .
```

恢复时先停 api/worker，解包到卷，再启动。备份包应校验大小与哈希，解压时防路径穿越。

### 6.3 密钥

数据库备份**不应**包含 `DAJIDALI_MASTER_KEY`。主密钥单独保管；恢复后需要重新配置
凭证或按运维手册轮换，不能把旧备份中的密文当作可直接使用。

## 7. 升级与回退

升级顺序（设计 12.4）：

1. 记录当前镜像标签与镜像 digest，完成数据库与附件备份；
2. 进入维护窗口，停止 `worker`、`scheduler`、`api`（proxy 可继续提供静态页）；
3. 修改 `DAJIDALI_VERSION` 或构建新镜像；
4. 只执行一次 `init-db`；
5. 用同一镜像标签启动 api/worker/scheduler；
6. 检查 `/api/v1/health/ready`、日志与任务队列；
7. 恢复任务，抽样验证读、写和任务执行。

```bash
docker compose -f deploy/compose.yaml stop worker scheduler api
docker compose -f deploy/compose.yaml --env-file deploy/.env build
docker compose -f deploy/compose.yaml --env-file deploy/.env run --rm api \
  python -m dajidali init-db
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d
```

回退：

- **不能**只把旧镜像连接到已升级的新库；先确认迁移结构向后兼容，否则恢复升级前备份。
- 推荐回退路径：停写 → 恢复升级前 PostgreSQL/附件备份 → 切回旧镜像 → 健康检查。
- 迁移的 `downgrade` 仅在明确验证过时使用；不要在正式库上盲目重试失败迁移。

## 8. 安全清单

- `deploy/.env` 权限限制为部署用户可读（如 `chmod 600`），不提交仓库。
- 只开放 proxy 端口；确认 `docker compose ps` 中 postgres/api 没有 `0.0.0.0` 端口映射。
- 使用 HTTPS 后再设 `DAJIDALI_HTTPS=1`；纯 HTTP 冒烟测试保持 0。
- 数据库密码、主密钥、AI/来源凭证不进镜像、不写日志、不出现在 URL。
- 定期更新基础镜像并记录 digest；升级前备份。
- 失败登录、会话撤销、备份恢复等安全事件应有审计；当前日志能力有限，见运维手册。

## 9. 未验证项

以下内容**未在真实 Docker/Linux/PostgreSQL 环境执行**，属于交付风险：

- `docker build`（node 前端构建、pnpm/corepack 网络、Python 依赖安装、psycopg 安装）。
- Compose 五个服务的真实启动、健康检查、重启策略、日志轮转和镜像复用。
- PostgreSQL 连接与并发：`requirements.lock` 未收录 `psycopg`，Dockerfile 在构建时额外安装
  `psycopg[binary]`；这破坏了锁文件可复现性，需在 G0 依赖锁中正式固定。
- HTTPS、Secure Cookie、真实域名/证书、Host/Origin 校验和外部反代行为。
- 命名卷在非 root（uid 10001）下的读写、bind mount 权限、备份/恢复演练。
- `nginx -t`、`docker compose config` 的真实校验与容器内静态资源缓存。
- 真实管理员登录、业务任务（worker/scheduler 处理器）与备份 API 的端到端流程。
- 5 名在线用户、真实回测期间的性能与内存指标。

## 10. 相关文档

- [本机快速开始](quick-start.md)
- [运维手册](operations.md)
- [设计文档 12.3/12.4](../superpowers/specs/2026-11-02-dajidali-bs-design.md)
