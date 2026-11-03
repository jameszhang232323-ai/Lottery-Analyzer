# syntax=docker/dockerfile:1
#
# 大吉大利发布镜像（设计文档 12.3）：
#   web-build 阶段：Node 构建 Vue 前端，产物只进入 nginx 镜像；
#   runtime   阶段：Python 运行 API/worker/scheduler，不含 Node；
#   proxy     阶段：非 root nginx 提供同源静态资源与 /api 反向代理。
#
# 本文件尚未在真实 Docker 环境中构建验证；镜像版本与 psycopg 安装为待验证项。

FROM node:22-alpine AS web-build
WORKDIR /build/web
RUN corepack enable && corepack prepare pnpm@10 --activate
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml web/.npmrc ./
RUN pnpm install --frozen-lockfile
COPY web/index.html web/tsconfig.json web/vite.config.ts web/vitest.config.ts ./
COPY web/src ./src
RUN pnpm build


FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    DAJIDALI_DATA_DIR=/data \
    DAJIDALI_MIGRATIONS_DIR=/app/migrations

# 非 root 运行；/data 的属主会在空命名卷初始化时保留。
RUN groupadd --system --gid 10001 dajidali \
    && useradd --system --uid 10001 --gid dajidali --home-dir /app --shell /usr/sbin/nologin dajidali \
    && mkdir -p /app /data \
    && chown -R dajidali:dajidali /app /data

WORKDIR /app
COPY backend/requirements.lock ./requirements.lock
# PostgreSQL 驱动（psycopg + psycopg-binary）已固定在依赖锁中，镜像与开发环境一致。
RUN pip install --no-cache-dir -r requirements.lock
COPY backend/src ./src
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini

USER dajidali
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3)"]

# 默认只跑 API；worker/scheduler 通过 compose command 覆盖。
CMD ["python", "-m", "dajidali", "serve-api", "--host", "0.0.0.0", "--port", "8000"]


FROM nginxinc/nginx-unprivileged:1.27-alpine AS proxy
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=web-build /build/web/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["wget", "-q", "--spider", "http://127.0.0.1:8080/healthz"]
