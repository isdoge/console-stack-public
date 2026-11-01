#!/bin/bash
# 用法: ./scripts/deploy.sh [--yes]
# 部署前自动备份当前状态，然后 git pull + 重建重启
#   --yes  跳过交互确认（供非交互 ssh / cron 使用）
# 首次 PG 割接（§8.3）：./scripts/deploy.sh --storage-cutover --target-commit <完整SHA> --yes
#   委托 backup_runtime.py cutover 外置控制器；已持有锁时沿用 FD，不递归再抢。
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- --storage-cutover 拦截：必须在常规 pull/重建流程之前路由 -------------------
CUTOVER=0
CUTOVER_TARGET=""
CUTOVER_STATE_DIR=""
prev=""
for arg in "$@"; do
  case "$arg" in
    --storage-cutover) CUTOVER=1 ;;
    --target-commit) ;;
    --target-commit=*) CUTOVER_TARGET="${arg#--target-commit=}" ;;
    --state-dir=*) CUTOVER_STATE_DIR="${arg#--state-dir=}" ;;
    --state-dir) ;;
    *)
      if [ "$prev" = "--target-commit" ]; then CUTOVER_TARGET="$arg"
      elif [ "$prev" = "--state-dir" ]; then CUTOVER_STATE_DIR="$arg"
      fi ;;
  esac
  prev="$arg"
done
if [ "$CUTOVER" = "1" ]; then
  if [ -z "${CUTOVER_TARGET:-}" ]; then
    echo "❌ --storage-cutover 需要 --target-commit <完整SHA>（§8.3 外置控制器契约）。已取消。" >&2
    exit 1
  fi
  case " $* " in
    *" --yes "*) ;;
    *) echo "❌ --storage-cutover 需要 --yes 确认。已取消。" >&2; exit 1 ;;
  esac
  CUTOVER_ARGS=(cutover "$PROJECT_DIR" --target-commit "$CUTOVER_TARGET" --yes)
  [ -n "$CUTOVER_STATE_DIR" ] && CUTOVER_ARGS+=(--state-dir "$CUTOVER_STATE_DIR")
  if [ -n "${CONSOLE_DEPLOY_LOCK_FD:-}" ]; then
    CUTOVER_ARGS+=(--lock-fd "$CONSOLE_DEPLOY_LOCK_FD")
  fi
  # V4-02/§8.3：准备阶段参数（--release-dir / --deploy-env / --instance-role /
  # --prepare-cmd / --skip-prepare）原样透传给外置控制器——wrapper 自己不做准备，
  # 只负责把「已审查候选 release + 生产 root + 最终 SHA」交给同一个控制器。
  prev=""
  for arg in "$@"; do
    case "$arg" in
      --storage-cutover|--yes|--target-commit|--state-dir) ;;
      --target-commit=*|--state-dir=*) ;;
      *)
        if [ "$prev" != "--target-commit" ] && [ "$prev" != "--state-dir" ]; then
          CUTOVER_ARGS+=("$arg")
        fi ;;
    esac
    prev="$arg"
  done
  exec python3 "$SCRIPT_DIR/backup_runtime.py" "${CUTOVER_ARGS[@]}"
fi

# Python owns the same real-path flock as rollback and the host timed backup.
# The shell inherits its descriptor so killing the parent cannot release a live deploy.
if [ -z "${CONSOLE_DEPLOY_LOCK_FD:-}" ]; then
  exec python3 "$SCRIPT_DIR/backup_runtime.py" deploy "$PROJECT_DIR" "$@"
fi
python3 "$SCRIPT_DIR/backup_runtime.py" check-lock "$PROJECT_DIR" "$CONSOLE_DEPLOY_LOCK_FD"

cd "$PROJECT_DIR"

ASSUME_YES=0
[ "$1" = "--yes" ] && ASSUME_YES=1

# 确认辅助：非交互（无 tty）且未给 --yes 时直接终止，避免 read 在 ssh 无 -t 下报错
confirm() {
  local prompt="$1"
  if [ "$ASSUME_YES" = "1" ]; then
    echo "$prompt [--yes 已指定，继续]"
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "$prompt"
    echo "❌ 非交互环境需要显式 --yes 才能继续。已取消。"
    exit 1
  fi
  read -rp "$prompt [y/N] " CONFIRM
  [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ]
}

# 外部 Git/网络偶发抖动不应要求人工从头重跑整批部署。
retry_command() {
  local attempts="$1"
  shift
  local attempt=1
  while [ "$attempt" -le "$attempts" ]; do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      echo "  ⚠ 命令失败（第 $attempt/$attempts 次），稍后重试..."
      sleep "$((attempt * 3))"
    fi
    attempt="$((attempt + 1))"
  done
  return 1
}

# NAS 的 GitHub SSH 默认经本地代理；若代理能接 TCP 却在 KEX 前断开，
# 用 DoH 取得 ssh.github.com 的真实 IP，绕过代理直连 443。仍使用现有
# GitHub SSH 密钥与 known_hosts 中的 github.com host key，不引入 token。
github_direct_git() {
  local remote_url direct_ip
  remote_url="$(git remote get-url origin 2>/dev/null || true)"
  case "$remote_url" in
    git@github.com:*|ssh://git@github.com/*) ;;
    *)
      echo "  ⚠ origin 不是 GitHub SSH 地址，不能使用直连回退"
      return 1
      ;;
  esac

  # DoH 端点链：cloudflare（境外）→ alidns（境内）。任一活着的端点给出
  # A 记录即可——单端点被 TLS 阻断时 deploy 不能跟着死（2026-10-25 实例：
  # NAS→cloudflare 443 reset，alidns 正常）。
  direct_ip=""
  for doh in \
    'https://cloudflare-dns.com/dns-query' \
    'https://223.5.5.5/resolve'; do
    direct_ip="$(
      curl -fsS --connect-timeout 5 --max-time 10 \
        -H 'accept: application/dns-json' \
        "$doh?name=ssh.github.com&type=A" \
        | python3 -c 'import json,sys; data=json.load(sys.stdin); print(next(x["data"] for x in data.get("Answer", []) if x.get("type") == 1))'
    )" && break
    direct_ip=""
  done
  [ -n "$direct_ip" ] || return 1

  echo "  ↪ 绕过本地 SSH 代理，直连 GitHub SSH 443（$direct_ip）"
  GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand=none -o HostName=$direct_ip -o HostKeyAlias=github.com -p 443" git "$@"
}

# rollback 分支收敛检测：rollback.sh 会把代码切到 rollback/<TAG> 分支，
# 此状态下 git pull 无上游会失败——先收敛回 main
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "main" ]; then
  echo "⚠️  当前在分支 $CURRENT_BRANCH（非 main），可能处于回滚状态。"
  if confirm "切回 main 再部署？"; then
    git checkout main
  else
    echo "已取消。手动处理: git checkout main && git pull"
    exit 0
  fi
fi

# 收编线上 UI 对手改的 YAML 台账（Git/UI 双写常态），避免 pull 覆盖。
# machines.yaml 自 Track I 起也支持 UI 写回，必须与 panels/assets 同等保护。
# settings.yaml 自 C10-b 起同理（设置页白名单写回）。
INCORPORATED=0
YAML_PATHS=(console/assets.yaml console/machines.yaml console/settings.yaml)
if [ -z "${CONSOLE_PANELS_HOST_PATH:-}" ]; then
  YAML_PATHS=(console/panels.yaml "${YAML_PATHS[@]}")
fi
if git status --porcelain -- "${YAML_PATHS[@]}" 2>/dev/null | grep -q .; then
  echo "📝 检测到 ${YAML_PATHS[*]} 本地改动，自动收编进仓库..."
  git add -- "${YAML_PATHS[@]}"
  if git commit -m "chore(nas): 收编线上 YAML 台账手改 $(date +%Y%m%d-%H%M%S)" -- "${YAML_PATHS[@]}"; then
    echo "  ✓ 已提交本地 YAML 手改"
    INCORPORATED=1
  else
    echo "  ⚠ 收编提交失败（检查 git user.name/user.email 是否已配置），中止部署以免覆盖线上配置"
    exit 1
  fi
fi

# 检查是否还有其他未提交改动
if ! git diff-index --quiet HEAD -- 2>/dev/null; then
  echo "⚠️  检测到未提交的本地改动:"
  git status --short --branch
  echo ""
  if ! confirm "是否继续部署？未提交的改动可能会被覆盖"; then
    echo "已取消。请先提交或暂存本地改动。"
    exit 0
  fi
fi

# 生成备份标签
TAG="$(date +%Y%m%d-%H%M%S)"
echo "📦 备份标签: $TAG"
echo ""

# 备份关键运行时文件 + 当前代码版本（供 rollback 连代码一起回滚）
# 复用回滚恢复前快照的发布路径；任一成员失败均中止，gitsha 最后原子发布。
python3 - "$TAG" "$CONSOLE_DEPLOY_LOCK_FD" <<'PY'
import sys
from pathlib import Path
from scripts.backup_runtime import YAMLS, snapshot_group, backup_dir

root, tag = Path.cwd(), sys.argv[1]
snapshot_group(root, tag, lock_fd=int(sys.argv[2]))
from scripts.backup_runtime import deployment_environment
print("  ✓ 数据库备份 (" + deployment_environment(root)["CONSOLE_STORAGE_ENGINE"] + ")")
for name in YAMLS:
    if (backup_dir(root) / f"{name}.{tag}").is_file():
        print(f"  ✓ {name}")
    else:
        print(f"  ⚠ {name} 不存在，跳过（兼容旧版本）")
print("  ✓ gitsha " + (backup_dir(root) / f"gitsha.{tag}").read_text().strip()[:8])
PY

# 只归档超过最新 10 组的正常日期标签；可恢复、可重入，不清理旧 trash。
python3 "$SCRIPT_DIR/backup_runtime.py" retire "$PROJECT_DIR"

echo ""
echo "⬇️  git pull (rebase，收编提交存在时本地 main 与 origin 分叉，裸 pull 会 fatal)..."
if ! retry_command 3 git pull --rebase; then
  echo "  ⚠ 常规 GitHub SSH 连续失败，尝试已认证的直连 443 回退..."
  retry_command 2 github_direct_git pull --rebase
fi

# 收编提交回推 origin，避免 NAS main 积累未 push 提交、mac 侧看不到线上手改
if [ "$INCORPORATED" = "1" ]; then
  if retry_command 3 git push origin main \
      || retry_command 2 github_direct_git push origin main; then
    echo "  ✓ 收编提交已推送 origin"
  else
    echo "  ⚠ 收编提交推送失败，请稍后手动 git push（不影响本次部署）"
  fi
fi

# D0-1：running commit 只能在构建期注入——镜像里没有 .git，容器内 git rev-parse
# 拿不到东西，ops_status 的 running_commit 长期恒 null。必须在 pull 之后取，
# 这样注进去的才是本次真正部署的那个 commit。
GIT_COMMIT="$(git rev-parse --short HEAD)"
export GIT_COMMIT
echo ""
echo "🏷️  注入 running commit: $GIT_COMMIT"

echo ""
echo "🚀 重建并重启服务..."
docker compose build console
python3 "$SCRIPT_DIR/backup_runtime.py" deploy-schema "$PROJECT_DIR" "$TAG" --lock-fd "$CONSOLE_DEPLOY_LOCK_FD"
# schema 变更后立即刷新完整控制器，避免宿主 timer 留在旧迁移基线；沿用部署锁。
python3 "$SCRIPT_DIR/backup_runtime.py" publish-runtime "$PROJECT_DIR" --lock-fd "$CONSOLE_DEPLOY_LOCK_FD"
docker compose up -d --no-build

# D0-3：部署留痕。backup/ 已只读挂进 console 容器，ops_status 读这份台账即可回答
# 「线上跑的哪个 commit / 对应哪组备份 / 迁移到第几号」。放在重建之后写，记录的才是
# 真正起来的那一版；构建失败时不会留下一份「部署成功」的假凭证。
python3 "$SCRIPT_DIR/backup_runtime.py" deploy-accept "$PROJECT_DIR" "$TAG" --lock-fd "$CONSOLE_DEPLOY_LOCK_FD"
# B7 runbook 阶段 4 P3：部署 .env 配了 CONSOLE_BACKUP_IDENTITY_FILE 时，把新镜像的组合身份
# 原子同步到宿主，定时/部署前备份据此写 manifest v2；未配置时跳过（现役 v1）。
python3 "$SCRIPT_DIR/backup_runtime.py" sync-identity "$PROJECT_DIR" --lock-fd "$CONSOLE_DEPLOY_LOCK_FD"

echo ""
echo "✅ 部署完成。服务状态:"
docker compose ps

echo ""
if [ "$CONSOLE_STORAGE_ENGINE" = "postgres" ]; then
  echo "PG 已开写的部署当前仅支持向前修复；恢复须另行停写并验收。"
else
  echo "如需回滚: ./scripts/rollback.sh $TAG"
fi
