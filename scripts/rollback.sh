#!/bin/bash
# 用法: ./scripts/rollback.sh <backup-tag> [--data-only]
# 预检 -> 共用互斥 -> 停 console -> 恢复前快照 -> Git/数据 -> 重建和验收。
# Python 控制器在切旧代码前已加载，失败恢复入口随 manifest 保存在统一宿主备份目录（NAS /opt/.bak/console-stack）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
exec python3 "$SCRIPT_DIR/backup_runtime.py" rollback "$PROJECT_DIR" "$@"
