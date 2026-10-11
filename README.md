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

两者共享 `/app/data/media_server_manager.sqlite3`。SQLite 必须位于同一主机的本地文件系统，不支持把数据库放在 NFS、SMB 或其他网络共享目录中。业务数据保存在数据库，TMDB 图片等大文件位于 `/app/data/cache`，可按数据库中的 `tmdb_images` 元数据重建。

## Docker Compose

1. 创建本地配置目录，并确保容器用户 UID `10001` 可写。
2. 修改 [compose.yaml](compose.yaml) 中的默认数据路径，或设置 `MSM_DATA_DIR=/本机绝对路径/data`。
3. 启动服务：

```bash
docker compose up -d
```

4. 访问 `http://服务器IP:8088`，创建首个本地管理员账号。密码至少 10 位。
5. 添加 Plex 服务器，并把服务器页面生成的带密钥 Webhook URL 填入 Plex Webhooks。

Media Server Manager 不再提供默认账号或固定会话密钥。首次启动会在 SQLite 数据库的 `settings` 表中生成会话密钥；旧版本配置目录中的 `session_secret` 文件只会在首次启动时导入一次并删除。通过 HTTPS 反向代理访问时设置 `MSM_COOKIE_SECURE=1`。

### 旧版本升级

停止服务后，先导入旧数据库：

```bash
docker compose run --rm media-server-manager-web \
  python -m media_server_manager_admin migrate-legacy \
  --source /app/legacy-config/media_server_manager.db \
  --destination /app/data/media_server_manager.sqlite3 \
  --encryption-key-file /app/secrets/msm.key \
  --report /app/data/backups/migration-report.json
docker compose up -d
```

导入过程以只读方式打开旧数据库，按表导入并校验行数、外键和 SQLite 完整性；源数据库不会被修改。目标数据库已存在时命令会拒绝覆盖。先用 `backup-db` 创建一致性备份，再执行迁移。旧版配置目录中的 `session_secret` 会在首次启动时导入数据库并删除。

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

数据目录默认为仓库中的 `data`，可通过 `MSM_DATA_DIR` 指向其他本地目录。`MSM_CONFIG_DIR` 和 `MSM_CONFIG_PATH` 仅作为迁移期间的兼容别名。

## CLI

安装后优先使用标准入口 `msm` 和 `msm-admin`。CLI 使用 WebUI 的同一数据库和 Worker，不再读取 `config.ini`：

```bash
# 为所有启用服务器入队并等待完成
msm --all

# 只处理指定服务器
msm --all --server-id 1

# 仅入队，不等待 Worker
msm --all --enqueue-only
```

管理员命令通过 `msm-admin` 执行数据库升级、旧库导入、备份恢复、完整性检查和密钥轮换：

```bash
msm-admin upgrade-db
msm-admin migrate-legacy --source ./config/media_server_manager.db --destination ./data/media_server_manager.sqlite3
msm-admin backup-db
msm-admin check-db
msm-admin rotate-secrets --old-key-file old.key --new-key-file new.key
```

`python -m media_server_manager_admin`、`python -m media_server_manager_web` 和 `python -m media_server_manager_worker` 仍作为兼容入口；`media-server-manager.py` 也继续转发到 `msm`。独立的 `--new` 模式已删除，新增项目统一由 WebUI 的带密钥 Webhook 接收。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MSM_DATA_DIR` | `./data` | 数据库、备份和可重建缓存目录 |
| `MSM_WEB_HOST` | `0.0.0.0` | Web 服务监听地址 |
| `MSM_WEB_PORT` | `8088` | Web 服务监听端口 |
| `MSM_WEB_THREADS` | `8` | Web 服务线程数 |
| `MSM_TIMEZONE` | `Asia/Shanghai` | Cron 和定时任务时区 |
| `MSM_WORKER_CONCURRENCY` | `2` | 可同时运行任务的服务器数 |
| `MSM_ITEM_WORKERS` | `4` | 单个本地化任务的项目并发数 |
| `MSM_JOB_RETENTION_DAYS` | `90` | 已结束任务保留天数 |
| `MSM_WEBHOOK_RETENTION_DAYS` | `30` | Webhook 事件保留天数 |
| `MSM_COOKIE_SECURE` | `0` | HTTPS 反向代理后设置为 `1` |
| `MSM_LOCAL_ENCRYPTION_KEY` | 无 | 生产环境必填，用于加密 Token、Webhook 和外部凭据 |
| `MSM_SECRET_KEY` | 自动生成 | 可选的外部会话密钥覆盖 |
| `MSM_CONFIG_PATH` | 已弃用 | 旧版 Docker Compose 配置目录别名 |
| `MSM_CONFIG_DIR` | 已弃用 | 旧版配置目录别名；迁移周期结束后移除 |
| `MSM_IMAGE` | `media-server-manager:latest` | Docker Compose 镜像 |
| `MSM_E2E` | 未设置 | 设置为 `1` 后运行浏览器端到端测试 |

Worker 每 5 秒写入心跳。WebUI 超过 15 秒未收到心跳会显示 Worker 离线；CLI 等待模式也会拒绝在 Worker 离线时入队。

## 开发

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
ruff check .
python3 -m mypy --explicit-package-bases src media_server_manager_web media_server_manager_worker media_server_manager_admin --ignore-missing-imports
MSM_DATA_DIR=./data alembic upgrade head
python3 scripts/check_schema.py
python3 scripts/check_architecture.py
python3 -m media_server_manager_admin check-db
python3 -m media_server_manager_admin backup-db
python3 -m media_server_manager_admin restore-db --source ./data/backups/media_server_manager-YYYYMMDD-HHMMSS.sqlite3
```

敏感字段密钥轮换使用一次性事务完成，旧值无法解密时会回滚：

```bash
python3 -m media_server_manager_admin rotate-secrets \
  --old-key-file old.key --new-key-file new.key
```

核心测试覆盖首次设置、CSRF、登录限速、Token 脱敏、Webhook 密钥、音乐库事件、标准 Cron、精确预览、冲突回滚和 Worker 公平调度。

新代码按 `src/media_server_manager` 的 API、应用服务、领域和基础设施层组织。旧版入口包在迁移期间保留兼容。数据库结构由 Alembic 管理，首次迁移会接管现有 schema，旧数据库可使用 `migrate-legacy` 导入到 `MSM_DATA_DIR`。

完整的停机迁移、密钥轮换、备份和恢复步骤见 [`docs/migration.md`](docs/migration.md)。

## 注意事项

- 使用 Plex 管理员账号的 Token，且不要向外部暴露配置目录或数据库备份。
- Webhook URL 中包含服务器专用密钥；轮换后必须同步更新 Plex 配置。
- Plex 不会为所有新增曲目发送事件，建议同时配置定时全量任务。
- 标题含日文假名时会跳过拼音排序。
- 继续观看工具会写入少量播放进度；执行前会重新检查候选状态，状态变化时自动跳过。

## 致谢

本项目参考了 [plex_localization_zhcn](https://github.com/sqkkyzx/plex_localization_zhcn) 和 [plexpy](https://github.com/anooki-c/plexpy)。
