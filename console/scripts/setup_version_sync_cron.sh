#!/usr/bin/env bash
# Install / uninstall the version_sync cron on the current host.
#
# Effect (install): every 30 min run `docker exec console-brain python version_sync.py`
# and append output to a log file. Idempotent: re-running never duplicates the
# job, and the exact legacy line below is migrated away.
#
# Ownership is exact, never substring: only the verbatim legacy line, or a job
# whose line ends with our full marker token, is touched. Comments, other
# containers, other project paths, similarly named scripts and marker prefixes
# (`...:version_sync-extra`) are preserved untouched.
#
# If the existing crontab cannot be read safely the script fails without
# writing anything and without creating the log directory.
#
# Usage:
#   ./setup_version_sync_cron.sh              # install (default)
#   ./setup_version_sync_cron.sh --install    # same
#   ./setup_version_sync_cron.sh --uninstall  # remove our line
#   ./setup_version_sync_cron.sh --dry-run    # print the plan, no changes
#   ./setup_version_sync_cron.sh -h|--help
#
# Runs on user crontab of the current login user; do NOT run as root unless
# root is the user managing console-stack cron.
#
# Q1（2026-09-09 owner 确认）：版本同步是**自动采集**，只由 authority 实例执行。
# 本脚本因此只应装在 NAS 上；Mac（client）装了也会被 Python 侧拒绝——
# `version_sync.sync_all_versions()` 在非 authority 时直接返回并打印一行说明，
# 不会抓 GitHub、不会写 versions 表。数据库层的维护锁是第二道保护。
# 装错位置时用 `./setup_version_sync_cron.sh --uninstall` 撤掉即可。
#
# Related: .agents/operations/version-sync.md — 版本同步与宿主 cron。

set -euo pipefail

MARKER="console-stack:version_sync"
LOG_DIR="${HOME}/.local/log/console-stack"
LOG_FILE="${LOG_DIR}/version_sync.log"
CRON_LINE="*/30 * * * * docker exec console-brain python version_sync.py >> ${LOG_FILE} 2>&1 # ${MARKER}"

# 2026-09-08 只读核验的 NAS 遗留行；整行 SHA256
# d0c2147447d98a77c93ed1d439e09a036599da0800cfc9b17f0b0cadbd12567f。
# 只有逐字节相同的行才被迁移；任何近似形式（注释、其他容器/路径、扩展 marker）都保留。
LEGACY_CRON_LINE="*/30 * * * * docker exec console-brain python version_sync.py >/dev/null 2>&1"

usage() {
    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//' | head -40
}

mode="install"
for arg in "$@"; do
    case "$arg" in
        --install)   mode="install" ;;
        --uninstall) mode="uninstall" ;;
        --dry-run)   mode="dry-run" ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; usage; exit 2 ;;
    esac
done

ensure_log_dir() {
    mkdir -p "$LOG_DIR"
}

# Print the current crontab; return 0 for an empty crontab (cron exits 1 with
# "no crontab for <user>"), non-zero for any other read failure. The caller
# must never write when this returns non-zero.
current_crontab() {
    local err out status
    err="$(mktemp)"
    status=0
    out="$(crontab -l 2>"$err")" || status=$?
    if [ "$status" -eq 0 ]; then
        rm -f "$err"
        printf '%s' "$out"
        return 0
    fi
    if [ "$status" -eq 1 ] && [ -z "$out" ] && grep -qi "no crontab" "$err"; then
        rm -f "$err"
        return 0
    fi
    rm -f "$err"
    return 1
}

# Our job is recognized only by the full marker token at end of line.
is_own_line() {
    case "$1" in
        *" # ${MARKER}") return 0 ;;
        *) return 1 ;;
    esac
}

# stdin: crontab text. Succeeds when at least one line is our own job.
has_own_line() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        if is_own_line "$line"; then
            return 0
        fi
    done
    return 1
}

# stdin: current crontab. stdout: crontab with our own jobs dropped, and — only
# when $1 is "legacy" — the verbatim legacy line too. Unrelated lines (and blank
# lines) are passed through untouched.
strip_lines() {
    local drop_legacy="${1:-}" line
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$drop_legacy" = "legacy" ] && [ "$line" = "$LEGACY_CRON_LINE" ]; then
            continue
        fi
        if is_own_line "$line"; then
            continue
        fi
        printf '%s\n' "$line"
    done
}

case "$mode" in
    dry-run)
        if ! existing="$(current_crontab)"; then
            echo "[dry-run] cannot read the existing crontab; refusing to plan" >&2
            exit 1
        fi
        if printf '%s\n' "$existing" | grep -Fxq "$LEGACY_CRON_LINE"; then
            echo "[dry-run] would migrate the legacy line:"
            echo "  $LEGACY_CRON_LINE"
        fi
        if printf '%s\n' "$existing" | has_own_line; then
            echo "[dry-run] would replace the existing marked job (idempotent)."
        fi
        echo "[dry-run] would ensure log dir: $LOG_DIR"
        echo "[dry-run] would ensure crontab line:"
        echo "  $CRON_LINE"
        echo "[dry-run] no directory created, no crontab written."
        ;;
    install)
        if ! existing="$(current_crontab)"; then
            echo "[install] cannot read the existing crontab; refusing to write" >&2
            exit 1
        fi
        ensure_log_dir
        migrated=0
        if printf '%s\n' "$existing" | grep -Fxq "$LEGACY_CRON_LINE"; then
            migrated=1
        fi
        # Rebuild without the legacy line and without any previous own job, then
        # append exactly one current job: repeated runs converge on this result.
        {
            if [ -n "$existing" ]; then
                printf '%s\n' "$existing" | strip_lines legacy
            fi
            printf '%s\n' "$CRON_LINE"
        } | crontab -
        if [ "$migrated" -eq 1 ]; then
            echo "[install] migrated the legacy line:"
            echo "  $LEGACY_CRON_LINE"
        else
            echo "[install] no legacy line found."
        fi
        echo "[install] ensured crontab line:"
        echo "  $CRON_LINE"
        echo "[install] log file: $LOG_FILE"
        ;;
    uninstall)
        if ! existing="$(current_crontab)"; then
            echo "[uninstall] cannot read the existing crontab; refusing to write" >&2
            exit 1
        fi
        if ! printf '%s\n' "$existing" | has_own_line; then
            echo "[uninstall] not present; nothing to do."
            exit 0
        fi
        printf '%s\n' "$existing" | strip_lines | crontab -
        echo "[uninstall] removed marker: $MARKER"
        if printf '%s\n' "$existing" | grep -Fxq "$LEGACY_CRON_LINE"; then
            echo "[uninstall] warning: an un-migrated legacy job is still installed:"
            echo "  $LEGACY_CRON_LINE"
        fi
        ;;
esac
