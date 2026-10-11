#!/bin/sh

set -eu

umask 077

mkdir -p "${MSM_DATA_DIR:-/app/data}"

# Schema changes are explicit and idempotent.  A failed migration stops the
# container before the web or worker process can touch a partially upgraded
# database; no reset or destructive recovery is attempted here.
python -m alembic upgrade head

# 运行 Python 脚本
exec "$@"
