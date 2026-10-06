# Media Server Manager

Media Server Manager 用于管理 Plex 媒体库、本地化元数据、同步 TMDB 目录，并对比服务器中已有与缺失的电影、剧集、季和分集资源。

## 功能

- 电影、电视剧、艺人、专辑、曲目和合集的拼音排序与搜索
- Genre、Style、Mood 标签中文映射及未映射标签扫描
- 精确变更预览、冲突检测和回滚
- 多 Plex 服务器、持久任务历史、实时进度和标准 Cron 定时
- 新增媒体 Webhook、通知渠道和连接诊断
- 媒体库同步、TMDB 数据对比、缺集展示和电视剧自动重检

标题排序字段会被锁定，以避免 Plex 刷新元数据后覆盖结果。回滚会恢复处理前的值和锁定状态；如果字段在处理后被用户修改，CLP 会报告冲突而不会覆盖。

## 架构

默认部署包含两个进程：

- `clp-web`：WebUI、API、OAuth、Webhook 和 SSE 实时状态
- `clp-worker`：任务执行、同服务器串行控制、定时任务、清理和心跳

两者共享 `/app/config/clp.db`。SQLite 必须位于同一主机的本地文件系统，不支持把数据库放在 NFS、SMB 或其他网络共享目录中。

## Docker Compose

1. 创建本地配置目录，并确保容器用户 UID `10001` 可写。
2. 修改 [compose.yaml](compose.yaml) 中的默认配置路径，或设置 `CLP_CONFIG_PATH=/本机绝对路径/config`。
3. 启动服务：

```bash
docker compose up -d
```

4. 访问 `http://服务器IP:8088`，创建首个本地管理员账号。密码至少 10 位。
5. 添加 Plex 服务器，并把服务器页面生成的带密钥 Webhook URL 填入 Plex Webhooks。

CLP 不再提供默认账号或固定会话密钥。首次启动会在配置目录生成权限为 `0600` 的 `session_secret`。通过 HTTPS 反向代理访问时设置 `CLP_COOKIE_SECURE=1`。

### v1 升级

v2 不迁移旧数据库。停止服务后执行：

```bash
docker compose run --rm clp-web python -m clp_admin reset-db --backup
docker compose up -d
```

旧数据库会重命名为带时间戳的备份文件；服务器、Token、任务和规则需要重新配置。Webhook URL 也必须替换为新格式。

## Python 运行

需要 Python 3.11 或更高版本：

```bash
python3 -m pip install -r requirements.txt
python3 -m clp_web
```

在另一个终端启动 Worker：

```bash
python3 -m clp_worker
```

配置目录默认为仓库中的 `config`，可通过 `CLP_CONFIG_DIR` 指向其他本地目录。

## CLI

CLI 使用 WebUI 的同一数据库和 Worker，不再读取 `config.ini`：

```bash
# 为所有启用服务器入队并等待完成
python3 -m clp --all

# 只处理指定服务器
python3 -m clp --all --server-id 1

# 仅入队，不等待 Worker
python3 -m clp --all --enqueue-only
```

`python3 chinese-localization-for-plex.py --all` 在 v2 中仍作为兼容入口保留。独立的 `--new` 模式已删除，新增项目统一由 WebUI 的带密钥 Webhook 接收。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CLP_CONFIG_DIR` | `./config` | 数据库和会话密钥目录 |
| `CLP_TIMEZONE` | `Asia/Shanghai` | Cron 和定时任务时区 |
| `CLP_WORKER_CONCURRENCY` | `2` | 可同时运行任务的服务器数 |
| `CLP_ITEM_WORKERS` | `4` | 单个本地化任务的项目并发数 |
| `CLP_JOB_RETENTION_DAYS` | `90` | 已结束任务保留天数 |
| `CLP_WEBHOOK_RETENTION_DAYS` | `30` | Webhook 事件保留天数 |
| `CLP_COOKIE_SECURE` | `0` | HTTPS 反向代理后设置为 `1` |
| `CLP_SECRET_KEY` | 自动生成 | 可选的外部会话密钥覆盖 |

Worker 每 5 秒写入心跳。WebUI 超过 15 秒未收到心跳会显示 Worker 离线；CLI 等待模式也会拒绝在 Worker 离线时入队。

## 开发

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
ruff check .
mypy clp clp_web clp_worker clp_admin
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
