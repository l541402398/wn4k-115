#!/usr/bin/env bash
# 启动转存工作台（Linux / macOS）
set -e
cd "$(dirname "$0")"
exec python -m app.server