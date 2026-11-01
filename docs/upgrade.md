# 升级、备份与回滚

前提：已按 [install.md](install.md) 安装。升级只做三件事：**先备份 → 换代码/镜像 → 管理员迁移后再起服务**。启动闸门从不自动迁移；`check` 不通过就不要启动新版本。

## 1. 备份（每次升级前）

```bash
mkdir -p backups
docker compose exec -T console-postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backups/console-$(date +%Y%m%d-%H%M%S).dump
ls -l backups/
```

## 2. 在线升级

```bash
git fetch origin
git checkout <新版本 commit 或 tag>        # 记下旧 commit：git rev-parse HEAD
# 镜像：A 已发布镜像 —— 把 .env 的 CONSOLE_IMAGE 改成新 sha-<commit> 后
docker compose pull console
#       B 源码构建 ——
# docker compose build console
docker compose run --rm console-schema migrate
docker compose run --rm console-schema check
docker compose up -d console
curl -fsS http://127.0.0.1:8081/api/health
```

## 3. 离线升级（目标机无外网 / 无 registry）

在**能联网**的机器上，使用专门准备离线包的干净检出，准备源码 bundle、镜像和校验文件。下面两段使用 Bash；任一步失败立即停止。`IMAGE` 必须取发布回执中与目标源码兼容的已发布镜像；纯文档提交可继续使用上一版镜像，不假设每个源码提交都有镜像 tag。

```bash
set -euo pipefail
SOURCE="$PWD/console-stack-public"
NEW="<新版本源码 commit 或 tag>"
IMAGE="ghcr.io/isdoge/console-stack-public:sha-<已发布镜像版本>"
git -C "$SOURCE" fetch origin
NEW=$(git -C "$SOURCE" rev-parse --verify "${NEW}^{commit}")
SHORT=$(printf '%.7s' "$NEW")
OUT="$PWD/console-stack-offline-$SHORT"
mkdir "$OUT"                            # 新建目录；不覆盖已有离线包

# bundle 必须导出命名引用；裸 commit SHA 不会成为 bundle 的导出头。
git -C "$SOURCE" switch --detach "$NEW"
git -C "$SOURCE" bundle create "$OUT/source.bundle" HEAD
git -C "$SOURCE" bundle verify "$OUT/source.bundle"
test "$(git bundle list-heads "$OUT/source.bundle" HEAD)" = "$NEW HEAD"
printf '%s\n' "$NEW" > "$OUT/commit.txt"

docker pull --platform linux/amd64 "$IMAGE"
printf '%s\n' "$IMAGE" > "$OUT/image.txt"
docker image save -o "$OUT/image.tar" "$IMAGE"
gzip "$OUT/image.tar"
(cd "$OUT" && shasum -a 256 source.bundle image.tar.gz commit.txt image.txt > SHA256SUMS)
# 首次离线安装还须另带 postgres:16-alpine 镜像；既有实例已具备。
```

把整个 `console-stack-offline-<短 SHA>` 目录拷到目标机。在既有安装的仓库根运行：

```bash
set -euo pipefail
OFFLINE="/path/to/console-stack-offline-<短 SHA>"
(cd "$OFFLINE" && shasum -a 256 -c SHA256SUMS)
NEW=$(cat "$OFFLINE/commit.txt")
SHORT=$(printf '%.7s' "$NEW")

# 先停应用，再备份，避免迁移期间继续写入；备份失败就停在此处排障。
docker compose stop console
mkdir -p backups
BACKUP="backups/console-$(date +%Y%m%d-%H%M%S).dump"
docker compose exec -T console-postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP"
test -s "$BACKUP"

git bundle verify "$OFFLINE/source.bundle"
git fetch "$OFFLINE/source.bundle" HEAD
test "$(git rev-parse FETCH_HEAD)" = "$NEW"
git switch --detach FETCH_HEAD
gunzip -c "$OFFLINE/image.tar.gz" > "$OFFLINE/image.tar"
docker image load -i "$OFFLINE/image.tar"
IMAGE=$(cat "$OFFLINE/image.txt")
docker image inspect "$IMAGE" >/dev/null
# 安装时可能未写 CONSOLE_IMAGE；统一替换或追加，不依赖该键原先存在。
awk -v image="$IMAGE" '!/^CONSOLE_IMAGE=/ { print } END { print "CONSOLE_IMAGE=" image }' .env > .env.offline-new
chmod 600 .env.offline-new
mv .env.offline-new .env

docker compose run --rm --pull never console-schema migrate
docker compose run --rm --pull never console-schema check
docker compose up -d --no-build --pull never console
curl --retry 20 --retry-connrefused --retry-delay 1 -fsS http://127.0.0.1:8081/api/health
```

`source.bundle` 用 `HEAD` 导出并按 `commit.txt` 核对目标提交；镜像从本地归档加载，`--pull never` 与 `--no-build` 保证目标机不需要 registry、npm 或 pip。失败时保留升级前 dump 和离线包，排障后再继续。

离线机器上**不要** `docker compose build`：前端 `npm ci` 与后端 `pip install` 需要包仓库。若必须离线源码构建，先在联网机器上用同一 lockfile 预热 npm/pip 缓存，前端命令为：

```bash
ROOT="$PWD"                                  # 仓库根
# 与 console/Dockerfile frontend-public 相同：只给合同级插件根（示例 plugins/audit 不进组合）
PLUG="$ROOT/.build-plugins"; rm -rf "$PLUG"; mkdir -p "$PLUG"
cp plugins/contract.json plugins/__init__.py "$PLUG/"
cp plugins/composition.public.json "$PLUG/composition.json"
export CONSOLE_COMPOSITION_FILE="$ROOT/instance/public/composition.json" CONSOLE_PLUGIN_ROOT="$PLUG"
cd kernel/frontend
npm ci
npm run typecheck      # 组合准备（prepare:composition）+ tsc
npm run build          # vite build，产物 dist/（无 .map）
```

## 4. 回滚

```bash
git checkout <旧 commit>            # 并把 .env 的 CONSOLE_IMAGE 改回旧 tag（或重新 build）
docker compose stop console
docker compose exec -T console-postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backups/<升级前的>.dump
docker compose run --rm console-schema check
docker compose up -d console
curl -fsS http://127.0.0.1:8081/api/health
```

`check` 会核对组件登记与实际表结构；回滚后若报版本超前（新版本追加过组件迁移），说明恢复的不是升级前备份。
