#!/bin/sh

umask 077

mkdir -p /app/config

# 运行 Python 脚本
exec "$@"
