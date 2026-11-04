# 本机快速开始

本文面向 Windows 本机单用户模式：SQLite、API、单 worker、单 scheduler，默认只监听
`127.0.0.1:8000`。浏览器通过同源 API 访问，不需要安装客户端。

> 结论状态：脚本语法、CLI 单元测试和 `init-db` 临时库迁移已做静态/命令级检查；
> **真实 Windows 首次启动、浏览器登录闭环、worker/scheduler 业务任务尚未在本环境实测**，
> 详见文末“未验证项”。请把本文当作操作手册而不是验收报告。

## 1. 前置条件

- Windows 10/11，PowerShell 5.1 或 PowerShell 7。
- Python 3.12 或更高版本；项目虚拟环境位于 `backend\.venv`。
- 运行本机模式**不需要 Node.js**，但**必须先**在 `web` 目录执行 `pnpm build`，
  生成 `web\dist\index.html`，再启动脚本；服务器模式由 nginx 提供静态资源，
  不需要设置 `DAJIDALI_STATIC_DIR`。
- 从源码首次准备：

```powershell
# 在项目根目录执行
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"

# 仅首次或前端代码变更后需要（需要 Node.js 与 pnpm）
cd web
pnpm install
pnpm build
cd ..
```

## 2. 启动

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File tooling\start_local.ps1 -OpenBrowser
```

脚本会依次：

1. 检查 `backend\.venv\Scripts\python.exe`、Python 版本和项目依赖；
2. 检查 `web\dist\index.html`；存在时把 `DAJIDALI_STATIC_DIR` 设为该目录的绝对路径，
   缺失时提示先构建，但**不会阻止后端启动**；
3. 检查 `data\run\*.pid` 是否已有本机服务，以及 8000 端口是否被占用；
4. 执行一次 `python -m dajidali init-db`；重复执行是空操作；
5. 隐藏窗口启动 API、worker、scheduler，PID 写入 `backend\data\run\*.pid`；
6. 轮询 `http://127.0.0.1:8000/api/v1/health/ready`，最多 60 秒；
7. 就绪后打开浏览器（`-OpenBrowser` 时）。

默认地址：<http://127.0.0.1:8000/>。API 文档：<http://127.0.0.1:8000/api/v1/docs>。

### 自定义端口或数据目录

```powershell
powershell -ExecutionPolicy Bypass -File tooling\start_local.ps1 -Port 8010 -OpenBrowser
powershell -ExecutionPolicy Bypass -File tooling\start_local.ps1 -DataDir D:\dajidali-data
```

自定义数据目录后，后续 `create-admin`/`stop_local.ps1` 也要带同一路径：

```powershell
backend\.venv\Scripts\python.exe -m dajidali create-admin `
  --data-dir D:\dajidali-data --username admin --password "请换成至少8位的密码"
powershell -ExecutionPolicy Bypass -File tooling\stop_local.ps1 -DataDir D:\dajidali-data
```

脚本使用绝对路径传参，中文和空格路径按其原始路径处理。

## 3. 创建管理员（首次）

服务启动后，另开一个终端：

```powershell
cd backend
.\.venv\Scripts\python.exe -m dajidali create-admin --username admin --password "请换成至少8位的密码"
```

- 用户名已存在时会明确报错，不会静默覆盖或重置密码。
- 密码只用于本次命令；不要把它写进脚本、截图或提交到仓库。命令行参数可能进入
  PowerShell 历史，生产环境应改用受控的密码注入方式（当前 CLI 尚未提供，见“未验证项”）。
- 如果提示数据库尚未初始化，先确认启动脚本的迁移步骤成功，再重试。

## 4. 停止

```powershell
powershell -ExecutionPolicy Bypass -File tooling\stop_local.ps1
```

脚本按 PID 停止 API、worker、scheduler 并清理 PID 文件；进程已经不存在时只提示并
清理旧 PID 文件，不会因此报错。

> 也可以直接双击项目根目录的 `启动服务.bat` / `停止服务.bat`，效果相同。

## 5. 在软件内配置 AI 凭证（推荐）

登录管理员账号后，进入 **设置与任务 → 数据源与 AI 状态**：

1. 首次使用前，部署者需要设置一次主密钥（用于加密数据库中的凭证）：

   ```powershell
   $env:DAJIDALI_MASTER_KEY = python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
   ```

   主密钥不落库、不入备份；丢失后已保存的凭证无法解密，需要重新填写。

2. 在表单里填写 **API 地址、模型名、API Key**，点「保存 AI 凭证」。
   凭证以 AES-256-GCM 加密写入数据库，界面只显示掩码（如 `sk-****mnop`）。
3. **重启 worker**（`stop_local.ps1` → `start_local.ps1`）后生效；启动日志会显示
   `已注册内置处理器：ai.analyze（来源=数据库）`。
4. 需要更换或删除时，用同一卡片上的「删除已保存凭证」按钮。

> 环境变量仍受支持且**优先级更高**（`DAJIDALI_AI_BASE_URL` / `DAJIDALI_AI_MODEL` /
> `DAJIDALI_AI_API_KEY`）。两者都配置时以环境变量为准。

## 6. 查看状态与日志

- 健康探针：`http://127.0.0.1:8000/api/v1/health/ready`
- 存活探针：`http://127.0.0.1:8000/api/v1/health/live`
- PID：`backend\data\run\api.pid`、`worker.pid`、`scheduler.pid`
- 日志：`backend\data\logs\api-*.out.log`、`*.err.log`（每次启动带时间戳）

```powershell
# 需要时可在服务运行时执行；会运行完整后端/前端检查并请求健康探针
powershell -ExecutionPolicy Bypass -File tooling\verify.ps1
```

`verify.ps1` 会调用现有 `tooling\check.ps1`，耗时较长；健康探针失败会给出非零退出码。

## 7. 常见提示

| 提示 | 含义与处理 |
|---|---|
| 端口 8000 已被占用 | 关闭占用端口的程序，或用 `-Port` 换端口；换端口后浏览器地址也要改 |
| 检测到 api/worker/scheduler 已在运行 | 先执行 `stop_local.ps1`；不要重复启动多个 worker/scheduler |
| 未找到 web\dist\index.html | 在 `web` 目录执行 `pnpm build`；不构建时后端仍可运行，但根路径不显示界面 |
| 数据库迁移失败 | 查看迁移命令输出和 `backend\data\logs`；不要跳过迁移直接启动 |
| API 未在 60 秒内就绪 | 查看 api 的 `.err.log`，确认端口、数据库路径和依赖；脚本会停止本次已启动的进程 |
| 登录请求成功但页面仍未登录 | 若设置了 `DAJIDALI_HTTPS=1` 却用 `http://` 访问，Secure Cookie 不会发送；本机 HTTP 应设为 0 |

## 8. 未验证项

以下项目**没有在本次开发环境中真实执行**，不能据此宣称本机交付已验收：

- Windows PowerShell 5.1 下真实启动/停止、隐藏窗口、中文/空格路径、端口冲突和重复启动的交互验证；本次只做了 `[ScriptBlock]::Create` 语法解析。
- 真实浏览器打开八页界面、登录、关闭浏览器后任务继续的完整闭环；依赖任务 11 的页面联调。
- worker/scheduler 的真实业务处理器：当前进程级注册表可能为空，未知任务类型会被标记失败；需要后续任务接入处理器后重测。
- `pnpm build` 在当前环境重新构建前端（仓库已有 `web/dist` 产物，但本次未重建）。
- 管理员创建后的登录、会话、Cookie 与 HTTPS 组合验证。
- 本机数据备份/恢复演练；操作步骤见 [operations.md](operations.md)，同样未实测。

## 8. 相关文档

- [服务器部署](deployment.md)
- [运维手册](operations.md)
- [项目 README](../../README.md)
