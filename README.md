# Media Server Manager

Media Server Manager 用于管理 Plex 媒体库、本地化元数据、同步 TMDB 目录，并对比服务器中已有与缺失的电影、剧集、季和分集资源。

Plex library localization, TMDB sync, and automation.

## 功能

- 电影、电视剧、艺人、专辑、曲目和合集的拼音排序与搜索
- Genre、Style、Mood 标签中文映射及未映射标签扫描
- 精确变更预览、冲突检测和回滚
- 多 Plex 服务器、持久任务历史、实时进度和标准 Cron 定时
- 新增媒体 Webhook、通知渠道和连接诊断
- 媒体库同步、TMDB 数据对比、缺集展示和电视剧自动重检

标题排序字段会被锁定，以避免 Plex 刷新元数据后覆盖结果。回滚会恢复处理前的值和锁定状态；如果字段在处理后被用户修改，MSM 会报告冲突而不会覆盖。

## 架构

默认部署包含两个进程：

- `media-server-manager-web`：WebUI、API、OAuth、Webhook 和 SSE 实时状态
- `media-server-manager-worker`：任务执行、同服务器串行控制、定时任务、清理和心跳

### 模块化架构（v3 基础）

项目现在同时提供面向后续生产扩展的模块化入口：

- `apps/api`：FastAPI、版本化 `/api/v1` 路由、JWT/OIDC 租户上下文和 RBAC。
- `apps/worker`：Celery Worker 入口，队列按 Plex I/O、媒体同步、目录同步和通知拆分。
- `apps/migrator`：Alembic 初始化和 SQLite 到 PostgreSQL 的首批服务器配置迁移工具。
- `packages/domain`、`packages/application`、`packages/infrastructure`、`packages/integrations`：领域、应用服务、数据库/队列基础设施和外部集成边界。
- `web`：React + TypeScript + Vite 独立前端骨架，开发服务器将 `/api` 代理到 FastAPI。

本地运行模块化 API：

```bash
python -m pip install -r requirements.txt
python -m apps.migrator.main --init
python -m apps.api
```

生产环境使用 PostgreSQL、Redis 和 Celery：

```bash
docker compose -f deploy/compose/docker-compose.modular.yml up --build
alembic upgrade head
```

模块化 API 使用共享表加 `tenant_id` 隔离租户；生产 PostgreSQL 迁移会为服务器和任务表启用 Row-Level Security。旧 Flask/SQLite 入口继续保留，用于兼容期和迁移。

两者共享 `/app/config/media_server_manager.db`。SQLite 必须位于同一主机的本地文件系统，不支持把数据库放在 NFS、SMB 或其他网络共享目录中。

## Docker Compose

1. 创建本地配置目录，并确保容器用户 UID `10001` 可写。
2. 修改 [compose.yaml](compose.yaml) 中的默认配置路径，或设置 `MSM_CONFIG_PATH=/本机绝对路径/config`。
3. 启动服务：

```bash
docker compose up -d
```

4. 访问 `http://服务器IP:8088`，创建首个本地管理员账号。密码至少 10 位。
5. 添加 Plex 服务器，并把服务器页面生成的带密钥 Webhook URL 填入 Plex Webhooks。

Media Server Manager 不再提供默认账号或固定会话密钥。首次启动会在 SQLite 数据库的 `settings` 表中生成会话密钥；旧版本配置目录中的 `session_secret` 文件只会在首次启动时导入一次并删除。通过 HTTPS 反向代理访问时设置 `MSM_COOKIE_SECURE=1`。

### v1 升级

v2 不迁移旧数据库。停止服务后执行：

```bash
docker compose run --rm media-server-manager-web python -m media_server_manager_admin reset-db --backup
docker compose up -d
```

旧数据库会重命名为带时间戳的备份文件；服务器、Token、任务和规则需要重新配置。Webhook URL 也必须替换为新格式。

## Python 运行

需要 Python 3.11 或更高版本：

```bash
python3 -m pip install -r requirements.txt
python3 -m media_server_manager_web
```

在另一个终端启动 Worker：

```bash
python3 -m media_server_manager_worker
```

配置目录默认为仓库中的 `config`，可通过 `MSM_CONFIG_DIR` 指向其他本地目录。

## CLI

CLI 使用 WebUI 的同一数据库和 Worker，不再读取 `config.ini`：

```bash
# 为所有启用服务器入队并等待完成
python3 -m media_server_manager --all

# 只处理指定服务器
python3 -m media_server_manager --all --server-id 1

# 仅入队，不等待 Worker
python3 -m media_server_manager --all --enqueue-only
```

`media-server-manager.py` 是命令行入口。独立的 `--new` 模式已删除，新增项目统一由 WebUI 的带密钥 Webhook 接收。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MSM_CONFIG_DIR` | `./config` | 数据库和可重建缓存目录 |
| `MSM_WEB_HOST` | `0.0.0.0` | Web 服务监听地址 |
| `MSM_WEB_PORT` | `8088` | Web 服务监听端口 |
| `MSM_WEB_THREADS` | `8` | Web 服务线程数 |
| `MSM_TIMEZONE` | `Asia/Shanghai` | Cron 和定时任务时区 |
| `MSM_WORKER_CONCURRENCY` | `2` | 可同时运行任务的服务器数 |
| `MSM_ITEM_WORKERS` | `4` | 单个本地化任务的项目并发数 |
| `MSM_JOB_RETENTION_DAYS` | `90` | 已结束任务保留天数 |
| `MSM_WEBHOOK_RETENTION_DAYS` | `30` | Webhook 事件保留天数 |
| `MSM_COOKIE_SECURE` | `0` | HTTPS 反向代理后设置为 `1` |
| `MSM_SECRET_KEY` | 自动生成 | 可选的外部会话密钥覆盖 |
| `MSM_CONFIG_PATH` | Compose 默认路径 | Docker Compose 配置目录 |
| `MSM_IMAGE` | `media-server-manager:latest` | Docker Compose 镜像 |
| `MSM_E2E` | 未设置 | 设置为 `1` 后运行浏览器端到端测试 |

Worker 每 5 秒写入心跳。WebUI 超过 15 秒未收到心跳会显示 Worker 离线；CLI 等待模式也会拒绝在 Worker 离线时入队。

## 开发

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
ruff check .
mypy media_server_manager media_server_manager_web media_server_manager_worker media_server_manager_admin
```

核心测试覆盖首次设置、CSRF、登录限速、Token 脱敏、Webhook 密钥、音乐库事件、标准 Cron、精确预览、冲突回滚和 Worker 公平调度。

## 注意事项

- 使用 Plex 管理员账号的 Token，且不要向外部暴露配置目录或数据库备份。
- Webhook URL 中包含服务器专用密钥；轮换后必须同步更新 Plex 配置。
- Plex 不会为所有新增曲目发送事件，建议同时配置定时全量任务。
- 标题含日文假名时会跳过拼音排序。
- 继续观看工具会写入少量播放进度；执行前会重新检查候选状态，状态变化时自动跳过。

## 致谢

本项目参考了 [plex_localization_zhcn](https://github.com/sqkkyzx/plex_localization_zhcn) 和 [plexpy](https://github.com/anooki-c/plexpy)。
