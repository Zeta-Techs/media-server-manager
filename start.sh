#!/bin/sh

umask 077

mkdir -p "${MSM_DATA_DIR:-/app/data}"

# 运行 Python 脚本
exec "$@"
