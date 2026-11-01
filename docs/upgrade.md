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

在**能联网**的机器上准备两个文件：

```bash
NEW=<新版本 commit>
git -C console-stack-public bundle create console-stack-public-$NEW.bundle $NEW
docker pull ghcr.io/isdoge/console-stack-public:sha-${NEW:0:7}
docker save ghcr.io/isdoge/console-stack-public:sha-${NEW:0:7} | gzip > console-stack-public-${NEW:0:7}.tar.gz
# 首次离线安装还需要：docker save postgres:16-alpine | gzip > postgres-16-alpine.tar.gz
```

拷到目标机后：

```bash
cd console-stack-public
git fetch /path/to/console-stack-public-$NEW.bundle $NEW && git checkout FETCH_HEAD
gunzip -c /path/to/console-stack-public-${NEW:0:7}.tar.gz | docker load
sed -i.bak "s#^CONSOLE_IMAGE=.*#CONSOLE_IMAGE=ghcr.io/isdoge/console-stack-public:sha-${NEW:0:7}#" .env && rm .env.bak
# 之后与在线升级相同：先备份，再 migrate → check → up -d → health
docker compose run --rm console-schema migrate
docker compose run --rm console-schema check
docker compose up -d console
curl -fsS http://127.0.0.1:8081/api/health
```

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
