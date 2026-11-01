# 安装（单机，Docker Compose）

适用：一台装有 Docker Engine 24+ 与 Compose v2 的 Linux/macOS 主机。全程不需要私人仓库、私人插件或任何生产凭据。
本页每条命令都在干净目录实跑过（见仓库发布回执）；与你环境不同的地方以报错为准，不要跳过 `check`。

## 0. 前置

```bash
docker version && docker compose version
openssl version   # 只用来生成随机密码，可用任意等价工具代替
```

## 1. 获取源码与配置

```bash
git clone https://github.com/isdoge/console-stack-public.git
cd console-stack-public

cp .env.example .env
# 随机生成 Flask 会话密钥与运行账号密码（写入 .env，不进 Git）
SK=$(openssl rand -hex 32); PW=$(openssl rand -hex 24)
sed -i.bak -e "s/^SECRET_KEY=.*/SECRET_KEY=$SK/" -e "s/^CONSOLE_PG_PASSWORD=.*/CONSOLE_PG_PASSWORD=$PW/" .env && rm .env.bak

# PG 管理员密码只放 secrets 文件（目录 700 保护宿主侧；文件 644 让容器内 postgres 用户可读）
mkdir -p secrets && chmod 700 secrets
openssl rand -hex 24 > secrets/pg-admin.password && chmod 644 secrets/pg-admin.password
```

## 2. 镜像：二选一

**A. 使用已发布镜像（推荐，不需要本地构建工具链）**

```bash
# 用不可变 tag：sha-<公共仓 commit 前 7 位>；可用 tag 见 GitHub Packages 页面或发布回执
echo "CONSOLE_IMAGE=ghcr.io/isdoge/console-stack-public:sha-<commit>" >> .env
docker compose pull console
```

**B. 从源码构建（target 固定为 `public`）**

```bash
docker compose build console
```

两种方式得到的镜像都只含 kernel、合同级空插件根与空台账；镜像内 `/app/LICENSE`、`/app/NOTICE`、`/app/THIRD_PARTY_LICENSES.txt` 为许可随附。

## 3. 初始化数据库（一次性）

```bash
docker compose up -d console-postgres
# 建组件登记表 + kernel 基线（新库路径；不会触碰任何既有遗产链）
docker compose run --rm console-schema migrate
# 建受限运行账号（名称须与 .env 的 CONSOLE_PG_USER 一致；密码取 .env 的 CONSOLE_PG_PASSWORD）
docker compose run --rm console-schema grant-app-role --role console_app
# 只读核对：输出 "ok": true 才继续
docker compose run --rm console-schema check
```

## 4. 启动与健康检查

```bash
docker compose up -d console
# 首次启动约 10–20 秒
curl -fsS http://127.0.0.1:8081/api/health
docker compose ps
```

浏览器打开 `http://127.0.0.1:8081/`。新实例没有机器与服务（空台账），首页为空态；在「机器」页添加，或用 Go agent（`kernel/agent`）注册。

## 5. 常见失败

| 现象 | 原因与处理 |
| --- | --- |
| `docker compose` 报 `缺 CONSOLE_PG_PASSWORD` | `.env` 未填运行账号密码（步骤 1） |
| `console` 退出码 3，日志提示 schema 不兼容 | 未执行步骤 3，或 `check` 未通过；启动闸门不会自动迁移 |
| `console` 退出码 2 / 认证失败 | `grant-app-role` 的 `--role` 与 `.env` 的 `CONSOLE_PG_USER` 不一致，或改过密码后未重跑 `grant-app-role` |
| postgres 起不来，提示读不到密码文件 | `secrets/pg-admin.password` 权限过严（见步骤 1 的 chmod） |

升级、备份与回滚见 [upgrade.md](upgrade.md)。
