# Media Server Manager 迁移运行手册

## 新目录和数据位置

正式 Python 包位于 `src/media_server_manager`。Web、Worker 和管理员旧模块只
保留兼容入口。生产环境只需要持久化挂载 `MSM_DATA_DIR`，默认布局为：

```text
data/
├── media_server_manager.sqlite3
├── cache/tmdb/
└── backups/
```

SQLite 文件必须位于本机文件系统；Web 和 Worker 必须共享同一个数据目录。

## 初始化和升级

```bash
MSM_DATA_DIR=./data python -m alembic upgrade head
MSM_DATA_DIR=./data python -m alembic check
```

容器启动脚本会执行同样的 `upgrade head`。迁移失败会阻止进程启动，不会删除或重建数据库。

## 导入旧库

先停止 Web 和 Worker，并保留源数据库原文件：

```bash
python -m media_server_manager_admin migrate-legacy \
  --source ./config/media_server_manager.db \
  --destination ./data/media_server_manager.sqlite3 \
  --encryption-key-file ./secrets/msm.key \
  --report ./data/backups/migration-report.json
```

导入会执行完整性、外键、行数、JSON、敏感字段和 TMDB 缓存校验。目标文件已存在时默认拒绝覆盖；可以先使用 `--dry-run`，失败导入使用 `--resume`。源库不会被删除。

导入按表提交，并在目标库的 `migration_runs` 记录当前表、已复制行数和失败原因。
因此进程中断或某个敏感字段解密失败时，修正密钥后重新执行下面的命令即可从失败表继续：

```bash
python -m media_server_manager_admin migrate-legacy \
  --source ./config/media_server_manager.db \
  --destination ./data/media_server_manager.sqlite3 \
  --encryption-key-file ./secrets/msm.key \
  --resume
```

也可以用 `--migration-id` 指定报告中的批次 ID。成功批次不能被 `--resume` 覆盖，
目标库完成后会生成 `.migration-complete.json` 标记文件。

## 备份、恢复和密钥轮换

```bash
python -m media_server_manager_admin backup-db
python -m media_server_manager_admin restore-db \
  --source ./data/backups/media_server_manager-YYYYMMDD-HHMMSS.sqlite3
python -m media_server_manager_admin check-db
python -m media_server_manager_admin rotate-secrets \
  --old-key-file ./secrets/old.key \
  --new-key-file ./secrets/new.key
```

`MSM_LOCAL_ENCRYPTION_KEY` 不写入数据库，也不应使用 `.env.example` 中的示例值。服务器 Token、Webhook Secret、OAuth Token、通知凭据和设置中的密钥在数据库中以 `enc:v1:` 格式保存。

## 迁移完成条件

- `ruff`、`mypy` 和完整测试通过。
- `alembic upgrade head` 可重复执行，`alembic check` 无差异。
- API、Application 和 Worker 目录不包含 DB-API SQL 或 SQLite 连接。
- CLI、备份和密钥轮换也通过 SQLAlchemy Session；仅为旧库导入和管理员迁移保留的 DB-API 兼容逻辑集中在 `infrastructure/db/legacy_sql.py` 和 `infrastructure/db/sql.py`。API、Application Service 和 Worker 运行路径不再接触 DB-API，`infrastructure/db/repositories/` 只包含带类型的 Repository。
- ORM metadata 与数据库的表、列和访问索引检查通过。
- 旧入口只转发到 `src/media_server_manager`。
- Docker Compose 只挂载 `/app/data`，不携带数据库、缓存、日志或依赖目录。
