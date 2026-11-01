#!/usr/bin/env bash
# Install / uninstall the monitoring maintenance cron lines on the current host.
#
# Effect (install): three docker-exec jobs against console-brain —
#   - retention (aggregate_hourly → cleanup_raw → cleanup_hourly) every 5 min
#   - evaluate  (run_evaluation shadow)                     every 1 min
#   - dms       (dead man's switch shadow)                  every 5 min
# All three run `python monitoring_maintenance.py <mode>` inside the container.
# Idempotent: re-running never duplicates jobs.
#
# Ownership is exact, never substring: only a job whose line ends with one of
# our full marker tokens is touched. Comments, other containers, other project
# paths, similarly named scripts and marker prefixes are preserved untouched.
#
# If the existing crontab cannot be read safely the script fails without
# writing anything and without creating the log directory.
#
# Usage:
#   ./setup_monitoring_maintenance_cron.sh              # dry-run (default)
#   ./setup_monitoring_maintenance_cron.sh --install    # install the 3 lines
#   ./setup_monitoring_maintenance_cron.sh --uninstall  # remove our lines
#   ./setup_monitoring_maintenance_cron.sh -h|--help
#
# Options:
#   --notify    install with --notify on the evaluate/dms lines (email ON).
#               Default is shadow mode: notify=False, no email is ever sent
#               until the operator confirms a single sender and reinstalls.
#               --notify only applies to --install; dry-run prints what would
#               be installed. It never executes the Python script itself.
#
# Runs on user crontab of the current login user; do NOT run as root unless
# root is the user managing console-stack cron.
#
# Q1 / PG-02: 监控维护是自动采集的一部分，只由 authority 实例执行。本脚本
# 因此只应装在 NAS 上；Mac（client）装了也会被 Python 侧拒绝——
# `monitoring_maintenance.run_mode()` 在非 authority 时直接返回并打印一行
# 说明，不写库、不发信。装错位置用 --uninstall 撤掉即可。
#
# 状态证据：Python 侧每轮把 started/finished/last_success/逐任务错误/结果
# 计数原子写到 CONSOLE_MONITORING_MAINTENANCE_JSON（容器内默认
# /tmp/console-monitoring/maintenance.json）。只装 cron 不等于在跑——验收看
# 该文件的 finished 时间戳，不看 cron 列表。
#
# Related: .agents/operations/version-sync.md — 同款宿主 cron 形态。

set -euo pipefail

MARKER_PREFIX="console-stack:monitoring-maintenance"
MARKER_RETENTION="${MARKER_PREFIX}:retention"
MARKER_EVALUATE="${MARKER_PREFIX}:evaluate"
MARKER_DMS="${MARKER_PREFIX}:dms"
LOG_DIR="${HOME}/.local/log/console-stack"
LOG_RETENTION="${LOG_DIR}/monitoring-retention.log"
LOG_EVALUATE="${LOG_DIR}/monitoring-evaluate.log"
LOG_DMS="${LOG_DIR}/monitoring-dms.log"

# --notify 只改 evaluate/dms 两条线；retention 无发信面，永远不加开关。
notify_arg=""
NOTIFY_EVALUATE=""
NOTIFY_DMS=""

usage() {
    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//' | head -45
}

mode="dry-run"
for arg in "$@"; do
    case "$arg" in
        --install)   mode="install" ;;
        --uninstall) mode="uninstall" ;;
        --dry-run)   mode="dry-run" ;;
        --notify)    notify_arg="--notify" ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; usage; exit 2 ;;
    esac
done

if [ -n "$notify_arg" ]; then
    NOTIFY_EVALUATE=" --notify"
    NOTIFY_DMS=" --notify"
fi

RETENTION_LINE="*/5 * * * * docker exec console-brain python monitoring_maintenance.py retention >> ${LOG_RETENTION} 2>&1 # ${MARKER_RETENTION}"
EVALUATE_LINE="* * * * * docker exec console-brain python monitoring_maintenance.py evaluate${NOTIFY_EVALUATE} >> ${LOG_EVALUATE} 2>&1 # ${MARKER_EVALUATE}"
DMS_LINE="*/5 * * * * docker exec console-brain python monitoring_maintenance.py dms${NOTIFY_DMS} >> ${LOG_DMS} 2>&1 # ${MARKER_DMS}"

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

# Our jobs are recognized only by one of the full marker tokens at end of line.
is_own_line() {
    case "$1" in
        *" # ${MARKER_RETENTION}") return 0 ;;
        *" # ${MARKER_EVALUATE}") return 0 ;;
        *" # ${MARKER_DMS}") return 0 ;;
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

# stdin: current crontab. stdout: crontab with all our own jobs dropped.
# Unrelated lines (and blank lines) are passed through untouched.
strip_lines() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        if is_own_line "$line"; then
            continue
        fi
        printf '%s\n' "$line"
    done
}

install_lines() {
    # Rebuild without any previous own jobs, then append exactly one current
    # job per mode: repeated runs converge on this result.
    {
        if [ -n "$existing" ]; then
            printf '%s\n' "$existing" | strip_lines
        fi
        printf '%s\n' "$RETENTION_LINE"
        printf '%s\n' "$EVALUATE_LINE"
        printf '%s\n' "$DMS_LINE"
    } | crontab -
}

case "$mode" in
    dry-run)
        if ! existing="$(current_crontab)"; then
            echo "[dry-run] cannot read the existing crontab; refusing to plan" >&2
            exit 1
        fi
        if printf '%s\n' "$existing" | has_own_line; then
            echo "[dry-run] would replace the existing marked jobs (idempotent)."
        fi
        echo "[dry-run] would ensure log dir: $LOG_DIR"
        echo "[dry-run] would ensure crontab lines:"
        echo "  $RETENTION_LINE"
        echo "  $EVALUATE_LINE"
        echo "  $DMS_LINE"
        echo "[dry-run] notify mode: ${notify_arg:-shadow (default, no email)}"
        echo "[dry-run] no directory created, no crontab written."
        ;;
    install)
        if ! existing="$(current_crontab)"; then
            echo "[install] cannot read the existing crontab; refusing to write" >&2
            exit 1
        fi
        ensure_log_dir
        install_lines
        echo "[install] ensured crontab lines:"
        echo "  $RETENTION_LINE"
        echo "  $EVALUATE_LINE"
        echo "  $DMS_LINE"
        echo "[install] log files under: $LOG_DIR"
        if [ -n "$notify_arg" ]; then
            echo "[install] NOTIFY ENABLED: evaluate/dms will send email."
        else
            echo "[install] shadow mode (default): notify=False, no email."
        fi
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
        echo "[uninstall] removed markers: ${MARKER_RETENTION} ${MARKER_EVALUATE} ${MARKER_DMS}"
        ;;
esac
