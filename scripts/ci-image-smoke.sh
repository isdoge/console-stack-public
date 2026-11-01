#!/usr/bin/env bash
# G06（提案 §11.3.2；PG-04 交接 §6.1 关闭标准）—— 最终镜像的集成冒烟。
#
# 关掉的是「docker job 假绿」：改造前 CI 的 docker job 只做
# `docker run -d ... && curl /api/health`，在 G06 启动闸门落地前，**没有任何 PG 配置**
# 的容器照样 health=200 绿灯——G06b 首红实测：无 PG 配置时 app 以 SQLite 启动，
# 并在工作副本里建出 163840 字节的 console.db。那种绿灯与生产形态无关。
#
# 现在这一步必须真起 PostgreSQL 16、真建 schema15、用**受限运行账号**（console_mac，
# NOSUPERUSER/NOCREATEDB/NOCREATEROLE，业务表 DML）启动最终镜像，逐条验证：
#   1. 空 schema 启动       → 退出码 3（schema 不兼容，fail closed，不自动迁移）
#   2. schema15 + 业务/诊断 → /api/health、/api/ledger/tx、/api/config/effective 全 200；
#                             后者确认 postgres/server16/schema15，书签真写读一个来回
#      client 的 /api/ops-status 必须转发 authority；隔离 authority 不可达时应 503。
#      插件业务先验证 authority 不可达时关闭，再用独立清单夹具验证启停及 PG 读写。
#   3. 容器内**没有** console.db（PG 引擎不落 SQLite 副本）
#   4. 未声明 CONSOLE_STORAGE_ENGINE → 退出码 4
#   5. 受限账号密码错误    → 退出码 2（连不上就停，不降级 SQLite）
#   6. client 未声明 CONSOLE_AUTHORITY_ORIGIN → 退出码 4
#   7. 镜像 Python = 3.10  （与生产 python:3.10-slim-bookworm 对齐，不以本机解释器代替）
#
# 本地与 CI 同一条命令：
#   docker build -f console/Dockerfile --target with-personal -t console-brain:ci .
#   bash scripts/ci-image-smoke.sh
#
# 环境变量：IMAGE（默认 console-brain:ci）。
# 凭据是一次性测试值，只存在于本脚本的一次性容器内，与生产无关。
set -euo pipefail

IMAGE="${IMAGE:-console-brain:ci}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SUFFIX="$$"
NET="console-ci-smoke-net-${SUFFIX}"
PG_NAME="console-ci-smoke-pg-${SUFFIX}"
APP_NAME="console-ci-smoke-app-${SUFFIX}"
AUTH_NAME="console-ci-smoke-authority-${SUFFIX}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/console-ci-smoke-XXXXXX")"
LOG="${TMP_DIR}/app.log"

ADMIN_USER="console_ci_admin"
ADMIN_PW="console_ci_smoke_admin_pw"
APP_ROLE="console_mac"
APP_PW="console_ci_smoke_app_pw"
DB="console_ci_smoke"

PASS=0
FAILED=0
ok() { printf '  [PASS] %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf '  [FAIL] %s\n' "$*"; FAILED=$((FAILED + 1)); }

cleanup() {
  docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
  docker rm -f "$AUTH_NAME" >/dev/null 2>&1 || true
  docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# macOS 没有 GNU timeout（coreutils 装的是 gtimeout）；负向用例必须有上限，
# 否则「拒绝启动」失败时会变成 CI 挂死。
# 用字符串而不是数组：macOS 自带 bash 3.2 在 `set -u` 下展开**空数组**会直接报
# unbound variable（本轮实跑踩到），CI 的 bash 5 不会——脚本要两边都能跑。
TIMEOUT_CMD=""
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD="timeout 120"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD="gtimeout 120"
else
  echo "  [WARN] 未找到 timeout/gtimeout：负向用例没有挂死上限"
fi
with_timeout() {
  if [ -n "$TIMEOUT_CMD" ]; then $TIMEOUT_CMD "$@"; else "$@"; fi
}

echo "=== 前置检查"
for tool in docker curl python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "[FAIL] 缺少 $tool"; exit 1; }
done
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "[FAIL] 镜像 $IMAGE 不存在；先 docker build -f console/Dockerfile --target with-personal -t $IMAGE ."
  exit 1
}
ok "镜像 $IMAGE 已就绪"

python_version="$(docker run --rm "$IMAGE" python -V 2>&1)"
case "$python_version" in
  "Python 3.10."*) ok "镜像解释器与生产对齐 —— $python_version" ;;
  *) bad "镜像解释器应为 Python 3.10（生产 python:3.10-slim-bookworm），实际 $python_version" ;;
esac

echo
echo "=== 一次性 PG16 + 受限账号"
# 四个 YAML 从仓库复制到临时目录再挂载：与 compose 的挂法同形，但容器即使回写
# 也写不进 worktree（settings.yaml 有白名单写回路径）。
# data/ 同样挂临时空目录（compose 挂的是真库目录）：**绝不**把线上 SQLite 库挂进来，
# 同时让「PG 引擎不落 SQLite 副本」这条判据有实际可写的落点，而不是空判。
for name in panels.yaml assets.yaml machines.yaml settings.yaml; do
  cp "${ROOT}/console/${name}" "${TMP_DIR}/${name}"
done
mkdir -p "${TMP_DIR}/data"
docker network create "$NET" >/dev/null
docker run -d --name "$PG_NAME" --network "$NET" \
  -e POSTGRES_USER="$ADMIN_USER" -e POSTGRES_PASSWORD="$ADMIN_PW" -e POSTGRES_DB="$DB" \
  postgres:16-alpine >/dev/null
# The official image bootstraps through a socket-only temporary server.
# Wait for TCP so its shutdown cannot be mistaken for the final server.
for _ in $(seq 1 60); do
  docker exec "$PG_NAME" pg_isready -h 127.0.0.1 -U "$ADMIN_USER" -d "$DB" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$PG_NAME" pg_isready -h 127.0.0.1 -U "$ADMIN_USER" -d "$DB" >/dev/null 2>&1 ||
  { echo "[FAIL] PG16 未在 60s 内就绪"; docker logs "$PG_NAME"; exit 1; }
ok "PG16 容器就绪（${PG_NAME}，库 ${DB}）"

admin_kv=(
  CONSOLE_PG_HOST="$PG_NAME" CONSOLE_PG_PORT=5432 CONSOLE_PG_DB="$DB"
  CONSOLE_PG_USER="$ADMIN_USER" CONSOLE_PG_PASSWORD="$ADMIN_PW"
)
app_kv=(
  CONSOLE_STORAGE_ENGINE=postgres
  # client：真实在线角色（Mac 页面形态）且非 authority——不触发任何采集线程，
  # CI 里不会有 tailscale/docker socket/ssh 的副作用。
  CONSOLE_INSTANCE_ROLE=client
  # 保留域名，非生产、非自身。先保持不可达以验证 fail closed；
  # 随后只在隔离网络内挂同名的插件清单 HTTP 夹具，不启动采集或连接生产 NAS。
  CONSOLE_AUTHORITY_ORIGIN=http://console-ci-authority.invalid:8080
  CONSOLE_PG_HOST="$PG_NAME" CONSOLE_PG_PORT=5432 CONSOLE_PG_DB="$DB"
  CONSOLE_PG_USER="$APP_ROLE" CONSOLE_PG_PASSWORD="$APP_PW"
)
admin_env=()
for kv in "${admin_kv[@]}"; do admin_env+=( -e "$kv" ); done
app_env=()
no_engine_env=()
no_authority_env=()
wrong_pw_env=()
for kv in "${app_kv[@]}"; do
  app_env+=( -e "$kv" )
  if [ "${kv%%=*}" != "CONSOLE_AUTHORITY_ORIGIN" ]; then
    no_authority_env+=( -e "$kv" )
  fi
  if [ "${kv%%=*}" != "CONSOLE_STORAGE_ENGINE" ]; then
    no_engine_env+=( -e "$kv" )
  fi
  if [ "${kv%%=*}" = "CONSOLE_PG_PASSWORD" ]; then
    wrong_pw_env+=( -e "CONSOLE_PG_PASSWORD=definitely-wrong" )
  else
    wrong_pw_env+=( -e "$kv" )
  fi
done
yaml_mounts=(
  -v "${TMP_DIR}/panels.yaml:/app/panels.yaml"
  -v "${TMP_DIR}/assets.yaml:/app/assets.yaml"
  -v "${TMP_DIR}/machines.yaml:/app/machines.yaml"
  -v "${TMP_DIR}/settings.yaml:/app/settings.yaml"
  -v "${TMP_DIR}/data:/app/data"
)

schema_cli() {  # 迁移/核对走镜像自带的 python + psycopg，仓库以只读挂入
  docker run --rm --network "$NET" "${admin_env[@]}" \
    -v "${ROOT}:/workspace:ro" -w /workspace "$IMAGE" python scripts/pg_schema.py "$@"
}

# 角色矩阵只建权限结构，密码由部署侧另外设置（§6.3）——CI 在这里补一次性密码。
schema_cli bootstrap-roles >"${TMP_DIR}/bootstrap.json" ||
  { echo "[FAIL] bootstrap-roles 失败"; cat "${TMP_DIR}/bootstrap.json"; exit 1; }
docker exec -e PGPASSWORD="$ADMIN_PW" "$PG_NAME" \
  psql -v ON_ERROR_STOP=1 -U "$ADMIN_USER" -d "$DB" -q \
  -c "ALTER ROLE ${APP_ROLE} LOGIN PASSWORD '${APP_PW}'" >/dev/null
ok "角色矩阵已建立、受限账号密码已设置"

# 负向用例的统一执行器：前台跑容器，判退出码。
run_app_expect_exit() {  # <描述> <期望码> <docker run 附加参数...>
  local desc="$1" expected="$2"
  shift 2
  local rc=0
  with_timeout docker run --rm --network "$NET" "$@" "$IMAGE" >"$LOG" 2>&1 || rc=$?
  if [ "$rc" = "$expected" ]; then
    ok "$desc —— 退出码 $rc"
  else
    bad "$desc —— 期望退出码 ${expected}，实际 ${rc}"
    sed -n '1,20p' "$LOG"
  fi
}

echo
echo "=== 负向：空 schema 必须拒绝启动（退出码 3）"
run_app_expect_exit "空库（无 schema15）" 3 "${app_env[@]}" "${yaml_mounts[@]}"
grep -q "schema" "$LOG" && ok "拒绝理由指向 schema 不兼容" || bad "启动日志里没有 schema 不兼容说明"
grep -qi "不自动迁移" "$LOG" && ok "明确声明不自动迁移" || bad "日志未声明不自动迁移"

echo
echo "=== 建 schema15（管理员 migrate，与部署同一条命令）"
schema_cli migrate >"${TMP_DIR}/migrate.json" ||
  { echo "[FAIL] migrate 失败"; cat "${TMP_DIR}/migrate.json"; exit 1; }
ok "migrate 完成 —— $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("applied=", d.get("applied_versions"), "version=", d["report"]["schema_version"])' "${TMP_DIR}/migrate.json")"
schema_cli check --app-role "$APP_ROLE" >"${TMP_DIR}/check.json" ||
  { echo "[FAIL] check 失败"; cat "${TMP_DIR}/check.json"; exit 1; }
ok "check --app-role $APP_ROLE 通过（受限账号权限面核对）"

echo
echo "=== 正向：受限账号启动最终镜像"
# 不发布宿主端口：探测一律走 `docker exec`（镜像自带 curl）。
# 理由是本轮实跑踩到的真实坑——宿主 127.0.0.1:18080 已被别的进程占用，
# 从宿主 curl 打过去的是那个进程（它同样返回 HTML 404），容器其实好好的，
# 判据却红了。exec 进容器探测既避开端口冲突，也证明请求确实落在被测容器里。
docker run -d --name "$APP_NAME" --network "$NET" "${app_env[@]}" "${yaml_mounts[@]}" "$IMAGE" >/dev/null
app_curl() { docker exec "$APP_NAME" curl -sS -m 10 "$@"; }
# 取「响应体 + 末行状态码」：curl 在连接失败时也会打印 000（-w 照常执行），
# 所以不要再 `|| echo 000` 兜一遍，否则失败信息会变成「000 000」。
fetch() {  # <url> [curl 参数...] → 打印 "<body>\n<code>"
  local url="$1"
  shift
  app_curl -w '\n%{http_code}' "$@" "$url" 2>/dev/null || true
}
split_body_code() {  # <fetch 输出> → BODY / CODE
  local resp="$1"
  CODE="${resp##*$'\n'}"
  BODY="${resp%$'\n'*}"
}

ready=0
for _ in $(seq 1 30); do
  if app_curl -fsS -o /dev/null http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$APP_NAME" 2>/dev/null)" != "true" ]; then
    break
  fi
  sleep 2
done
if [ "$ready" != 1 ]; then
  bad "镜像未能以受限账号在 schema15 上就绪"
  docker logs "$APP_NAME" 2>&1 | tail -30
  echo "--- 汇总：$PASS 通过 / $FAILED 失败"; exit 1
fi
ok "/api/health 200（受限账号 + schema15 + PG）"

split_body_code "$(fetch http://127.0.0.1:8080/api/ledger/tx)"
if [ "$CODE" = "503" ] && printf '%s' "$BODY" | python3 -c '
import json, sys
sys.exit(0 if json.load(sys.stdin).get("error") == "无法确认 NAS 插件状态，请稍后重试" else 1)
'; then
  ok "/api/ledger/tx 503（无法确认权威插件状态时关闭）"
else
  bad "/api/ledger/tx 权威不可达门禁失败，实际 ${CODE}"
fi

# 期望 schema 版本从 pg_migrations 派生（不硬编码）——新增迁移后烟测不腐烂。
EXPECT_SCHEMA="$(python3 -c '
import sys
sys.path.insert(0, "'"${ROOT}"'/console/backend")
import pg_migrations
print(pg_migrations.latest_schema_version())
')"
export EXPECT_SCHEMA
split_body_code "$(fetch http://127.0.0.1:8080/api/config/effective)"
if [ "$CODE" = "200" ] && printf '%s' "$BODY" | python3 -c '
import json, os, sys
expect = int(os.environ["EXPECT_SCHEMA"])
data = json.load(sys.stdin)
database = data.get("database") or {}
valid = (
    data.get("storage_engine") == "postgres"
    and data.get("schema_version") == expect
    and database.get("reachable") is True
    and database.get("schema_version") == expect
    and str(database.get("server_version", "")).split(".")[0] == "16"
)
sys.exit(0 if valid else 1)
'; then
  ok "/api/config/effective 200（本地存储诊断：postgres/server16/schema${EXPECT_SCHEMA} 可达）"
else
  bad "/api/config/effective 存储诊断失败（code=${CODE}，不回显有效配置）"
fi

split_body_code "$(fetch http://127.0.0.1:8080/api/ops-status)"
if [ "$CODE" = "503" ] && printf '%s' "$BODY" | python3 -c '
import json, sys
data = json.load(sys.stdin)
sys.exit(0 if data.get("error") == "NAS 权威端不可达，请稍后重试" else 1)
'; then
  ok "/api/ops-status 503（client 无隔离 authority，不伪造本地运维状态）"
else
  bad "/api/ops-status 转发边界失败（code=${CODE}）"
fi

# 真写读一个来回：POST 建书签 → GET 必须读到 → DELETE 清掉。
# 这一步同时证明受限账号对**业务表**有 DML，而不只是能连上。
#
# client 的共享 PG 业务必须先读取权威插件状态。这里只提供清单，不伪造业务响应；
# 账本与书签的实际请求仍在被测镜像执行，读写上面的隔离 PG16。
mkdir -p "${TMP_DIR}/authority/api"
python3 - "$ROOT" "${TMP_DIR}/authority/api/plugins" <<'PY'
import json
import sys
from pathlib import Path

rows = [
    {"id": json.loads(path.read_text())["id"], "enabled": True, "visible": True}
    for path in sorted((Path(sys.argv[1]) / "plugins").glob("*/manifest.json"))
]
Path(sys.argv[2]).write_text(json.dumps(rows))
PY
docker run -d --name "$AUTH_NAME" --network "$NET" \
  --network-alias console-ci-authority.invalid \
  -v "${TMP_DIR}/authority:/fixture:ro" -w /fixture \
  "$IMAGE" python -c 'from http.server import SimpleHTTPRequestHandler, test; SimpleHTTPRequestHandler.guess_type = lambda self, path: "application/json"; test(HandlerClass=SimpleHTTPRequestHandler, port=8080)' >/dev/null
authority_ready=0
for _ in $(seq 1 30); do
  if docker exec "$AUTH_NAME" curl -fsS -o /dev/null http://127.0.0.1:8080/api/plugins; then
    authority_ready=1
    break
  fi
  sleep 1
done
[ "$authority_ready" = 1 ] || { bad "隔离插件清单未就绪"; exit 1; }

split_body_code "$(fetch http://127.0.0.1:8080/api/ledger/tx)"
if [ "$CODE" = "200" ] && printf '%s' "$BODY" | python3 -c 'import json, sys; json.load(sys.stdin)' 2>/dev/null; then
  ok "/api/ledger/tx 200（权威启用，业务 API 走 PG）"
else
  bad "/api/ledger/tx 期望 200 + JSON，实际 ${CODE}"; printf '%s\n' "$BODY" | head -3
fi

set_fixture_ledger() {
  python3 - "${TMP_DIR}/authority/api/plugins" "$1" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rows = json.loads(path.read_text())
for row in rows:
    if row["id"] == "ledger":
        row["enabled"] = sys.argv[2] == "true"
path.write_text(json.dumps(rows))
PY
}
set_fixture_ledger false
split_body_code "$(fetch http://127.0.0.1:8080/api/ledger/tx)"
if [ "$CODE" = "503" ] && printf '%s' "$BODY" | python3 -c '
import json, sys
data = json.load(sys.stdin)
sys.exit(0 if data.get("code") == "plugin_disabled" and data.get("plugin") == "ledger" else 1)
'; then
  ok "/api/ledger/tx 503/plugin_disabled（权威停用即时生效）"
else
  bad "/api/ledger/tx 停用门禁失败，实际 ${CODE}"
fi
set_fixture_ledger true
split_body_code "$(fetch http://127.0.0.1:8080/api/ledger/tx)"
[ "$CODE" = "200" ] && ok "账本重新启用后无需重启即恢复" || bad "账本重新启用后期望 200，实际 ${CODE}"

# body 必须先构造再以变量传入，**不要**把带 `\"` 转义的 JSON 字面量直接内联在
# `"$( ... )"` 里：macOS 自带的 bash 3.2 在那个组合下会把 body 按逗号拆成多段
# （首尾花括号一起吃掉），`fetch` 于是一次变三次、每次只发一小段 JSON，app 回
# 400（waitress 的 HTML 错误页），后面两条判据跟着误红——run4 就是这么红的。
# 同一份脚本在 bash 5.2（容器内实测）不复现，属 3.2 专属；但「本地与 CI 同一条
# 命令」是这一步的前提，所以按两种 bash 都对的最保守写法来。
probe_id="ci-smoke-${SUFFIX}"
probe_body="$(printf '{"id":"%s","title":"ci-smoke","url":"https://example.invalid/"}' "$probe_id")"
split_body_code "$(fetch http://127.0.0.1:8080/api/bookmarks -X POST \
  -H 'Content-Type: application/json' \
  -d "$probe_body")"
if [ "$CODE" = "201" ] && printf '%s' "$BODY" | python3 -c '
import json, sys
data = json.load(sys.stdin)
sys.exit(0 if (data.get("data") or {}).get("id") == sys.argv[1] else 1)
' "$probe_id"; then
  ok "POST /api/bookmarks 201（受限账号写 PG 业务表）"
else
  bad "POST /api/bookmarks 期望 201，实际 ${CODE}"; printf '%s\n' "$BODY" | head -3
fi

split_body_code "$(fetch http://127.0.0.1:8080/api/bookmarks)"
if [ "$CODE" = "200" ] && printf '%s' "$BODY" | python3 -c '
import json, sys
data = json.load(sys.stdin)
rows = (data.get("data") or {}).get("list") or []
sys.exit(0 if any(row.get("id") == sys.argv[1] for row in rows) else 1)
' "$probe_id"; then
  ok "GET /api/bookmarks 读回刚写入的行（PG 读写全链路）"
else
  bad "GET /api/bookmarks 未读回写入的行（code=${CODE}）"; printf '%s\n' "$BODY" | head -3
fi

split_body_code "$(fetch "http://127.0.0.1:8080/api/bookmarks/${probe_id}" -X DELETE)"
[ "$CODE" = "200" ] && ok "DELETE /api/bookmarks 清理成功" || bad "DELETE /api/bookmarks 期望 200，实际 ${CODE}"

# data/ 是可写的临时目录（挂载点存在，可写），所以这条不是空判：
# G06b 首红时无 PG 配置的容器正是在这个位置建出了 163840 字节的 console.db。
if [ -z "$(docker exec "$APP_NAME" sh -c 'ls -A /app/data' 2>/dev/null)" ]; then
  ok "data/ 目录为空：PG 引擎不落 SQLite 副本"
else
  bad "容器在 data/ 里落了文件（PG 引擎下应为空）"
  docker exec "$APP_NAME" ls -lR /app/data || true
fi
docker rm -f "$APP_NAME" >/dev/null

echo
echo "=== 负向：启动闸门（未声明引擎 / 未声明 authority / 账号不可用）"
run_app_expect_exit "未声明 CONSOLE_STORAGE_ENGINE" 4 "${no_engine_env[@]}" "${yaml_mounts[@]}"
grep -q "CONSOLE_STORAGE_ENGINE" "$LOG" && ok "拒绝理由点名缺失的配置键" || bad "日志未点名 CONSOLE_STORAGE_ENGINE"

run_app_expect_exit "未声明 CONSOLE_AUTHORITY_ORIGIN" 4 "${no_authority_env[@]}" "${yaml_mounts[@]}"
grep -q "client 未配置 CONSOLE_AUTHORITY_ORIGIN" "$LOG" && ok "拒绝理由点名 client 缺失 authority" || bad "日志未点名 client 缺失 CONSOLE_AUTHORITY_ORIGIN"

run_app_expect_exit "受限账号密码错误" 2 "${wrong_pw_env[@]}" "${yaml_mounts[@]}"
grep -qi "sqlite" "$LOG" && ok "拒绝时明确不走 SQLite 降级" || bad "日志未声明不降级 SQLite"

echo
echo "=== 汇总"
echo "  判据 ${PASS}/$((PASS + FAILED)) 通过"
[ "$FAILED" = 0 ] || exit 1
