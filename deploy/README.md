# 部署文件

正式部署文件位于：

- `docker/Dockerfile`：使用仓库根目录作为构建上下文，例如 `docker build -f deploy/docker/Dockerfile .`。
- `compose/compose.yaml`：Web 和 Worker 共享 `/app/data` 的 SQLite 单机部署。

生产环境必须通过 `MSM_ENV=production` 和 `MSM_LOCAL_ENCRYPTION_KEY` 注入稳定的加密密钥；密钥不写入数据库，也不要提交 `.env` 文件。首次部署前先在宿主机创建并保护 `${MSM_DATA_DIR}`，升级前执行 `msm-admin backup-db`。

根目录的 `Dockerfile` 和 `compose.yaml` 保留为旧部署命令的兼容入口；两份配置应保持一致。
