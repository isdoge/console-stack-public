# ---- 前端：公共 zero（合同级空插件清单，不含私人实现）----
FROM node:24-alpine AS frontend-public
WORKDIR /workspace/kernel/frontend
COPY kernel/frontend/package.json kernel/frontend/package-lock.json* ./
# 与 CI frontend job 统一 Node 24；现有 jsdom 30 要求该分支至少 24.15。
# npm ci 严格按 lock 解析，不在镜像构建时更新依赖。
RUN npm ci
COPY kernel/frontend/ ./
COPY theme /workspace/theme
# 仅合同级插件根 + 空清单；不把 ledger/panels/personal_workbench 等打进公共包。
COPY plugins/contract.json plugins/__init__.py /workspace/plugins/
COPY plugins/composition.public.json /workspace/plugins/composition.json
COPY instance/public /workspace/instance/public
# 公共构建必须显式组合文件（profile=public）；插件根指向本 stage 合同级目录。
ENV CONSOLE_COMPOSITION_FILE=/workspace/instance/public/composition.json
RUN npm run build

# ---- 前端：私人全量（现役七插件清单）----
FROM node:24-alpine AS frontend-personal
WORKDIR /workspace/kernel/frontend
COPY kernel/frontend/package.json kernel/frontend/package-lock.json* ./
RUN npm ci
COPY kernel/frontend/ ./
COPY theme /workspace/theme
COPY plugins /workspace/plugins
RUN npm run build

# ---- 同发行版 Node 运行时（六工具，无 npm / dev server） ----
FROM node:24-bookworm-slim AS workbench-node

# ---- 后端运行（默认 public：不含私人 tools/personal-workbench / 私人插件）----
# Docker CLI apt 源固定为 bookworm，运行基镜像也必须固定同一发行版；
# 不使用会随上游漂移到 trixie 的 python:3.10-slim 浮动标签。
# personal-workbench 源码仅在 with-personal stage 打入；公共/独立构建选默认或
# --target public，不再要求构建上下文存在该目录（勿整树 COPY tools/）。
FROM python:3.10-slim-bookworm AS public

# git：版本追踪；docker-cli：容器启停查询；openssh-client：远程机器动作
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y git curl ca-certificates gnupg openssh-client libstdc++6 \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL --retry 5 --retry-all-errors --retry-delay 2 \
        --connect-timeout 10 --max-time 60 \
        https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

COPY --from=workbench-node /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

COPY console/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 公共运行层只落 kernel 真身到 /app（CMD python app.py 直接加载真身，
# 不再依赖 console shim 的 parents[2] 仓库布局）。私人桥接/Beszel 等仅存在于
# console/backend 的模块不进默认 public 镜像；见下方 with-personal。
COPY kernel/backend/ /app/

# 公共运行层：合同级空插件根，不含私人插件路径。
COPY plugins/contract.json plugins/__init__.py /app/plugins/
COPY plugins/composition.public.json /app/plugins/composition.json

# 空台账（列表）：公共镜像不打入私人 machines/assets；私人 compose bind-mount 覆盖。
RUN printf '%s\n' '[]' > /app/machines.yaml \
    && printf '%s\n' '[]' > /app/assets.yaml \
    && printf '%s\n' '[]' > /app/panels.yaml \
    && printf '%s\n' '{}' > /app/settings.yaml

COPY instance/public/image-composition.json /app/public-composition.json
ENV CONSOLE_INSTANCE_PROFILE=public
ENV CONSOLE_COMPOSITION_FILE=/app/public-composition.json

COPY --from=frontend-public /workspace/kernel/frontend/dist /app/static

# D0-1（C1 前置）：running commit 构建期注入。镜像里没有 .git，容器内 git rev-parse
# 拿不到任何东西，ops_status 的 running_commit 因此长期恒 null。deploy.sh 在 build 前
# 导出 GIT_COMMIT，compose 经 build.args 传进来；裸 docker build 不传时留 dev。
# 放在末尾是为了不让 commit 变化击穿上面的 apt/pip/npm 层缓存。
ARG GIT_COMMIT=dev
ENV GIT_COMMIT=${GIT_COMMIT}

ENV FLASK_ENV=production
EXPOSE 8080

CMD ["python", "app.py"]

# ---- 私人生产/CI：全量插件 + workbench 源码 + 私人前端包 ----
FROM public AS with-personal
# 清掉 public 组合闸门，恢复私人 legacy：compose bind-mount console/*.yaml 即可。
ENV CONSOLE_INSTANCE_PROFILE=private
ENV CONSOLE_COMPOSITION_FILE=
# 叠加仅存在于 console/backend、kernel 未覆盖的私人生产模块（不含 test_*/conftest）。
# 含 Beszel 适配/回填，以及 ledger/feed/renewals/hindsight 的旧导入 shim（实现仍在 plugins）。
COPY console/backend/beszel_api.py \
     console/backend/beszel_client.py \
     console/backend/capacity_legacy.py \
     console/backend/feed_aggregator.py \
     console/backend/hindsight_proxy.py \
     console/backend/ledger.py \
     console/backend/ledger_ai_classify.py \
     console/backend/ledger_auto_classify.py \
     console/backend/ledger_build_table.py \
     console/backend/ledger_cli.py \
     console/backend/ledger_cmb_parser.py \
     console/backend/ledger_cmb_sync.py \
     console/backend/ledger_file_routes.py \
     console/backend/ledger_files.py \
     console/backend/ledger_import.py \
     console/backend/ledger_merge_bills.py \
     console/backend/ledger_transfer.py \
     console/backend/monitor_observability.py \
     console/backend/monitoring_backfill.py \
     console/backend/renewals.py \
     /app/
COPY plugins /app/plugins
COPY tools/personal-workbench /app/tools/personal-workbench
COPY --from=frontend-personal /workspace/kernel/frontend/dist /app/static
# 私人镜像层内预置仓库台账，避免未挂载时只剩公共空文件；生产仍以 bind-mount 为准。
COPY console/assets.yaml /app/assets.yaml
COPY console/machines.yaml /app/machines.yaml
COPY console/panels.yaml /app/panels.yaml
COPY console/settings.yaml /app/settings.yaml

# 默认最终 stage = public（无私人路径）。私人 compose/CI 显式 --target with-personal。
FROM public
