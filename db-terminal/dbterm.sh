#!/usr/bin/env bash
# 汇英斋数据库终端启动脚本
cd "$(dirname "$0")" || exit 1
exec python3 dbterm.py "$@"
