# console-agent — console-stack 薄 Go 采集端（B4-1）

只读采集端：周期采集主机与 Docker 容器指标，经身份认证上报到 Console 接收端
（`POST /api/monitoring/v1/ingest`，B4-3 提供）。断网时本地 JSONL 暂存，
恢复后按**真实采样时间**补传。不执行任意命令，不写 Docker。

## 构建

```bash
cd agent
go build -o console-agent .     # 需 Go 1.21+；依赖仅标准库 + gopsutil/v3
go test ./...
go vet ./...
```

交叉编译（NAS 为 linux/arm64 时）：

```bash
GOOS=linux GOARCH=arm64 go build -o console-agent-linux-arm64 .
```

## 子命令

| 命令 | 作用 |
|---|---|
| `run` | 采集主循环（前台，交给 systemd 托管） |
| `enrol` | 首次注册（`--enroll-token` 必填），密钥持久化到 `<state-dir>/identity.json`（0600） |
| `install` | 写入并启用 `/etc/systemd/system/console-agent.service`（含 systemd 加固项） |
| `uninstall` | 停止并移除 service 单元；state 目录与身份保留 |
| `upgrade` | 下载升级包（`--url` + `--sha256`），SHA256 不符拒绝安装，装后自检失败自动回退备份 |
| `version` | 打印 agent 版本与协议版本 |

## 配置

优先级（后者覆盖前者）：内置默认 → `/etc/console-agent/config.yaml` → 环境变量 → 命令行 flag。

配置文件内容为 JSON（JSON 是 YAML 1.2 子集，故沿用 `.yaml` 扩展名）：

```json
{
  "server_url": "http://100.64.0.10:8081",
  "machine_id": "nas",
  "state_dir": "/var/lib/console-agent",
  "docker_socket": "/var/run/docker.sock",
  "sample_interval_sec": 300,
  "buffer_max_bytes": 104857600
}
```

| 配置项 | 环境变量 | flag | 默认 |
|---|---|---|---|
| server_url | `CONSOLE_AGENT_SERVER` | `-server` | `http://127.0.0.1:8081` |
| machine_id | `CONSOLE_AGENT_MACHINE_ID` | `-machine-id` | 无（必填，与台账一致） |
| state_dir | `CONSOLE_AGENT_STATE_DIR` | `-state-dir` | `/var/lib/console-agent` |
| docker_socket | `CONSOLE_AGENT_DOCKER_SOCKET` | — | `/var/run/docker.sock` |
| sample_interval_sec | `CONSOLE_AGENT_SAMPLE_INTERVAL` | `-sample-interval` | 300（5 分钟；可调 60 排障） |
| buffer_max_bytes | `CONSOLE_AGENT_BUFFER_BYTES` | `-buffer-bytes` | 100MB |
| enrol_token | `CONSOLE_AGENT_ENROLL_TOKEN` | `-enroll-token` | 无 |

## 首次安装

```bash
# 1. 放置二进制与配置
install -m 755 console-agent /usr/local/bin/
mkdir -p /etc/console-agent && cp config.yaml.example /etc/console-agent/config.yaml

# 2. 首次注册（enrol 令牌由 Console 侧生成）
console-agent enrol --machine-id nas --server http://100.64.0.10:8081 --enroll-token <token>

# 3. 安装 systemd 服务并启动
console-agent install
journalctl -u console-agent -f
```

卸载：`console-agent uninstall`（身份/缓冲文件保留在 state 目录，可手动清除）。

## 上报协议（v1）

每次采样为一个样本（JSONL 行），批次上报形状：

```json
{
  "protocol": "v1",
  "agent_version": "0.1.0",
  "machine_id": "nas",
  "sample_interval_sec": 300,
  "samples": [ { "protocol": "v1", "machine_id": "nas", "sampled_at": "2026-10-24T01:41:15Z", "seq": 42,
                 "host": { "cpu_used_pct": 12.4, "mem_total_bytes": 8589934592, "...": 0 },
                 "containers": [ { "name": "beszel", "cpu_pct": 2.1, "...": 0 } ] } ]
}
```

要点：

- **三处身份**：`machine_id`（台账 id，enrol 绑定）+ HTTP 头 `Authorization: Bearer <secret>`
  （enrol 时 Console 签发）+ 每样本 `machine_id`（接收端交叉校验）。
- **版本协商**：`protocol`（v1）+ `agent_version` 随每次请求上报；接收端越界返回
  426，agent 记录日志并停止重试同批次（等待升级）。
- **时间语义**：`sampled_at` 是源机器实际采样时间（ISO Z），缓冲补传不改写；
  `seq` 单调递增，重启后起点取当前时间低 16 位（时间前进保证重启后仍大于旧序列，
  接收端以 `machine_id + sampled_at + seq` 幂等去重）。
- **缺失显式**：主机整组失败则无样本；容器组失败样本缺 `containers` 字段；
  单项失败进 `errors` map。0 只出现在合法为 0 的字段（CPU、速率等）。
- **单位自描述**：字段名带单位（`_pct`/`_bytes`/`_bytes_sec`），内存/磁盘用 bytes
  （不沿用 Beszel 的 GB deprecated 口径），容器 CPU 用 `%`（同帧 read/preread
  窗内 cpu/precpu 差分，整机容量口径不乘核数，100% = 全部算力，与 beszel-agent
  同口径；`cpu_delta_sec` 是该容器的差分窗秒数，0 = 无有效窗，cpu_pct 无测量意义）。
- **网卡计数器**：逐网卡累计计数器（bytes，重启归零）+ agent 差分出的速率
  （`rates`，归零/新接口跳过该网卡，不产生负值）。计费网卡挑选归接收端/账本。

## 缓冲与资源约束

- 断网/失败时样本写 `<state-dir>/buffer.jsonl`（0600，原子写）；
  上报成功 Ack 删除，失败保留 pending 并在下次 Take 前回队头（宁可重复上报，
  接收端幂等去重）。
- 缓冲字节上限默认 100MB（`buffer_max_bytes`）；溢出丢最旧样本并累加
  `dropped_total`（随下一样本上报，缺口显式、不静默）。
- 崩溃残留的半行在重启时被 compact 丢弃（按坏行计数）。
- 开销控制：采样间隔 5 分钟；CPU 测量窗阻塞 ≤60s；容器 stats 并发 4；
  单样本约 0.5–4KB，稳态磁盘 = 缓冲上限；无采样时段进程休眠，CPU 趋近 0，
  常驻内存为运行时基线（实测约 10–20MB，< 50MB 预算）。

## 升级

```bash
console-agent upgrade --url https://example.com/console-agent-0.2.0 --sha256 <hex>
```

流程：下载到 `<state-dir>/upgrade/` → SHA256 校验（不符即删、拒绝安装）→
备份当前二进制 → 原子替换 → 运行 `console-agent version` 自检 →
失败回退备份。systemd 的 `Restart=always` 负责拉起新版本。

## 边界（与 B4 契约对齐）

- 只读：仅调用 `GET /containers/json`、`GET /containers/{id}/stats?stream=false`
  与 gopsutil 只读接口；socket 的 `:ro` 挂载不是只读保证，行为约束在代码——
  本 agent 不含任何 Docker 写调用。
- 容器稳定键 = `machine_id + name`（重建沿用同名即同一逻辑容器）；
  `id` 仅供排障。`compose_project` 取 Docker label，缺失即未分组。
- 密钥轮换/吊销：接收端吊销后上报返回 401，agent 日志明示需重新 enrol；
  轮换交叠期由接收端支持双密钥窗口（B4-3 契约），agent 侧无状态。

## 目录

```
agent/
├── go.mod / go.sum
├── main.go              # 子命令与主循环
├── collector/
│   ├── payload.go       # 上报样本 schema（单位/缺失语义）
│   ├── host.go          # gopsutil 主机采集 + 网卡差分速率
│   └── docker.go        # Docker Engine API（unix socket，只读）
├── sender/
│   ├── buffer.go        # JSONL 缓冲队列（溢出/崩溃恢复/pending 回滚）
│   ├── http.go          # enrol / 批量上报 / 版本协商
│   └── upgrade.go       # 升级包下载、SHA256 校验、回退
├── config/
│   └── config.go        # 文件 → env → flag 分层配置
└── README.md
```
