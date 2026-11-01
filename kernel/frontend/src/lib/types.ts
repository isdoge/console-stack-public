/** 机器标识符：来自 machines.yaml 台账 id（nas/txy/aly/us/kr/sg），另有 HOME_MACHINES 的 win/mac */
export type MachineId = 'nas' | 'txy' | 'aly' | 'win' | 'mac' | string;

export type AccessKey = 'home' | 'tailscale' | 'domain';

export interface AccessProbeEntry {
  configured: boolean;
  ok: boolean | null;
  msg?: string;
}

export interface AccessProbe {
  home?: AccessProbeEntry;
  tailscale?: AccessProbeEntry;
  domain?: AccessProbeEntry;
}

export interface ServiceAccess {
  home?: string;
  tailscale?: string;
  domain?: string;
  probe?: Partial<Record<AccessKey, boolean | null>>;
}

export interface ServiceHealth {
  status?: 'Healthy' | 'Unhealthy' | 'Unknown';
  msg?: string;
  /** T3（§4.4）：探活成功且剩余天数可信时才返回；失败时两个字段均不出现，错误文案只留 msg */
  ssl_ok?: boolean;
  ssl_days?: number;
}

export interface VersionInfo {
  local?: string;
  upstream?: string;
  behind?: number;
  status?: string;
}

export interface ReverseProxyRoute {
  listen: string[];
  hosts: string[];
  paths: string[];
  upstreams: string[];
  source?: 'host' | 'container' | string;
}

export type Priority = 'high' | 'medium' | 'low';

export type ServiceAction = 'start' | 'stop' | 'restart' | 'logs' | 'upgrade';

/** 入口层：域名 → 边缘 → tailnet → 家庭内网（C5 → L4「入口维度」）。 */
export type EntryLayer = 'domain' | 'edge' | 'tailscale' | 'home';

export interface ServiceEntry {
  /** 公网域名（含 scheme）；没配为 null。内网地址**不在**这个投影里 */
  domain: string | null;
  /** 'caddy' = 在 aly Caddy 的 upstream 名单里；null = 不在，或数据源未知 */
  edge: 'caddy' | string | null;
  /** false = C11 快照不可用，边缘层是**未知**而不是「没有」 */
  edge_known: boolean;
  tailscale: boolean;
  home: boolean;
  /** 有序层级，前端按它渲染链路 */
  layers: EntryLayer[];
}

export interface ServiceProject {
  repo?: string;
  links?: Array<{ label: string; href: string }>;
}

export interface Service {
  /** 实例台账登记的项目资料；框架不根据机器名或服务名推导。 */
  project?: ServiceProject;
  type?: string;
  systemd_unit?: string;
  systemd_scope?: 'system' | 'user';
  name: string;
  machine: MachineId;
  /** Docker 容器名；与 name 不同时用于状态匹配与合并 */
  container?: string;
  port?: number | string;
  /** T12 分支实验：健康时序（旧→新，最多 12 条），status ∈ Healthy|Unhealthy */
  health_history?: Array<{ ts: string; status: string }>;
  purpose?: string;
  priority?: Priority | string;
  note?: string;
  deploy?: string;
  tags?: string[];
  running?: 'running' | 'stopped' | string;
  health?: ServiceHealth;
  access?: ServiceAccess;
  proxy?: {
    provider?: string;
    target?: {
      machine?: MachineId;
      container?: string;
    };
    sources?: Array<'host' | 'container' | string>;
  };
  proxy_routes?: ReverseProxyRoute[];
  proxy_error?: string;
  /** C5 → L4 入口维度（后端 entry_view 投影，零探测；内网地址不在其中） */
  entry?: ServiceEntry;
  /** Docker 缺省支持启停/重启/日志；process 需显式配置；[] 禁止所有动作 */
  actions?: ServiceAction[];
  version?: {
    repo_url?: string;
    enabled?: boolean;
    kind?: 'image' | 'source' | 'http' | string;
    track_upstream?: boolean;
  };
  version_info?: VersionInfo;
  /** L1 三态：false = 关闭探测；对象 = 有限语义检查；缺省 = 按 port/type 默认推断 */
  health_probe?: boolean | HealthProbeSpec;
  /** compose 项目绝对路径；登记后该服务才可点 upgrade */
  compose_dir?: string;
  /** compose 文件名覆盖（不含路径分隔符）；缺省 compose.yaml */
  compose_file?: string;
}

/** L1 语义检查（设计 §5.5）。刻意没有 header / 凭据 / 脚本字段。 */
export interface HealthProbeSpec {
  type: 'http';
  /** 引用 access.<target> 里已有的地址，不另写 URL（避免第二份事实源） */
  target: 'domain' | 'tailscale' | 'home';
  /** `null` = 显式不校验状态码；**省略 = 后端按默认 200 校验**（最小配置也得查点什么） */
  expected_status?: number | null;
  max_latency_ms?: number;
  /** 固定字符串匹配，不是正则 */
  body_contains?: string;
}

export type HealthProbeMode = 'off' | 'default' | 'http';

export interface ServiceFormValues {
  type?: string;
  systemd_unit?: string;
  systemd_scope?: 'system' | 'user';
  actions?: ServiceAction[];
  name: string;
  machine: string;
  port: string;
  purpose: string;
  priority: string;
  note: string;
  deploy: string;
  home: string;
  tailscale: string;
  domain: string;
  repo_url: string;
  version_enabled: boolean;
  track_upstream: boolean;
  /** L1 三态：关闭 / 默认推断 / HTTP 语义检查 */
  health_probe_mode: HealthProbeMode;
  health_probe_target: HealthProbeSpec['target'];
  /** 表单里都按字符串存，提交时才归一化（空串 = 不校验该条） */
  health_probe_status: string;
  health_probe_latency: string;
  health_probe_body: string;
  tags: string[];
  /** Docker 容器名覆盖；与 name 不同时用于状态匹配 */
  container?: string;
  /** compose 项目绝对路径；登记后该服务才可点 upgrade */
  compose_dir?: string;
  /** compose 文件名覆盖（不含路径分隔符）；缺省 compose.yaml */
  compose_file?: string;
}

// ---- 机器台账（§4.2 GET /api/machines；字段名以 §4 为准）----

export type MachineRole = 'server' | 'node' | string;

export interface MachineRenewal {
  id: string;
  status?: RenewalStatus;
  expires_at?: string;
  days_left?: number;
  /** §7.2 契约 3（F 出后端）：价格 chip 与剩余价值用。cost=0 → 显示「免费」 */
  cost?: number;
  currency?: string;
  cycle?: string;
}

/**
 * M1：月底出站预测。**为 null 表示不该显示预测**（观测不足 72 小时、账本没
 * `since`、或本月已过完），前端此时什么都不画，不要退化成 0。
 */
export interface TrafficProjection {
  /** 按已观测速率外推到整月的出站量 */
  projected_gb: number;
  daily_gb: number;
  observed_hours: number;
  remaining_days: number;
  /** true = 账本不是从月初起算的，UI 必须标注「从首次采样起」 */
  partial_month: boolean;
  quota_gb: number | null;
  projected_pct: number | null;
  over_quota: boolean;
}

/** E1：月流量账本摘要（无配额或未采样时接口返回 null）。 */
export interface MachineTraffic {
  /** UTC 自然月，如 2026-08 */
  month: string;
  /** 计费网卡；空串表示自动挑选 */
  nic: string;
  sent_gb: number;
  recv_gb: number;
  used_gb: number;
  /** 无配额（nas）时后端回 null：只显累计、不画条不告警（§7.2 契约 4） */
  quota_gb: number | null;
  pct: number | null;
  /** 起算时间（装 agent 那天或上次月度翻页） */
  since?: string | null;
  updated_at?: string | null;
  /** 计数器归零次数，判断数据可信度用 */
  resets?: number;
  /** M1 月底预测；观测不足 72 小时为 null（不显示，不是显示 0） */
  projection?: TrafficProjection | null;
}

/**
 * M2：单机磁盘增长趋势（`GET /api/capacity`）。
 *
 * `days_to_full` 为 null 有三种成因（不在涨 / 拟合不稳 / 算出来超过一年），
 * 后端刻意不区分——UI 一律显示「暂不预测」。**上线时六台机器全部落在这一档
 * 是正确行为**，见后端 `capacity.py` 模块注释里的实测表。
 */
export interface DiskTrend {
  samples: number;
  span_hours: number;
  first_at: string;
  last_at: string;
  disk_total_gb: number;
  disk_used_gb: number;
  disk_free_gb: number;
  /** 日增 GB；负数表示在腾空间 */
  daily_gb: number;
  /** 拟合优度 0–1；低于 0.5 时后端不给 days_to_full */
  r_squared: number | null;
  trend: 'rising' | 'falling' | 'flat' | string;
  days_to_full: number | null;
}

/** M2 接口载荷；取数失败时 `ok=false` + machines 为空对象，不抛错。 */
export interface CapacityPayload {
  ok: boolean;
  ttl: number;
  /** Beszel 历史粒度，当前是 480m（约 30 天保留） */
  history_type: string;
  /** 后端判「拟合太差不给耗尽日期」的阈值；前端只用来解释原因，不重新判定 */
  min_r_squared: number | null;
  /** machine id → 趋势；样本不足的机器**不出现在这里** */
  machines: Record<string, DiskTrend>;
  error: string | null;
  /** B6-3：业务数据源回显（console_pg 分支带） */
  source?: string;
}

/** E4：一次探测结果。latency_ms 只有 up 时有值。 */
export interface ProbeSample {
  ts: string;
  status: 'up' | 'down' | string;
  latency_ms: number | null;
}

/** E3：node 白名单动作（后端只给 key + 文案，命令不外泄）。 */
export interface MachineAction {
  key: string;
  label: string;
}

export interface Machine {
  id: string;
  label: string;
  role: MachineRole;
  provider?: string;
  /** Tailscale IP（§4.1） */
  tailscale?: string;
  ssh?: string;
  /** .ssh-console/ 内密钥文件名（§4.1） */
  ssh_key?: string;
  beszel_name?: string;
  capabilities?: string[];
  /** node 机器公网域名（§4.1） */
  public_host?: string;
  /** §7.2 契约 3（F 出后端）：如 "Ubuntu 24.04"、"Debian 13"；缺失时 header 整块不渲染 */
  os?: string;
  /** §7.2 契约 3：emoji 旗，如 "🇰🇷"。刻意用 emoji 不引图标包 */
  region?: string;
  probe?: { type: string; port: number };
  /** 家宽公网探测配置：从 NAS 直连 public_host 的该端口（不改变 probe 语义）；未配为 null */
  public_probe?: { type: string; port: number } | null;
  traffic_quota_gb?: number;
  renewal_ref?: string;
  /** 探活结果：up | down | unknown（无 probe 配置的机器恒 unknown，§4.2） */
  probe_status?: string;
  probe_msg?: string;
  probe_ts?: string;
  /** 关联到期提醒；无关联为 null（§4.2） */
  renewal?: MachineRenewal | null;
  /** E1 月流量；无配额或未采样为 null */
  traffic?: MachineTraffic | null;
  /** E4 最近探测（旧→新），最多 20 条 */
  probe_history?: ProbeSample[];
  /** 家宽公网探测结果：up | down | unknown。只读后台 5 分钟一轮的缓存；
   *  本机代理没放行（fake-ip / 握手快得不可能）时是 unknown，不是 up */
  public_probe_status?: string;
  public_probe_msg?: string;
  public_probe_ts?: string | null;
  /** 家宽公网最近探测（旧→新），与 probe_history 分开存 */
  public_probe_history?: ProbeSample[];
  /** E3 可用节点动作；非 node 为空数组 */
  actions?: MachineAction[];
  /** B3 编辑表单用：计费网卡（列表接口不带） */
  traffic_nic?: string;
  /** J-B（§4.1）：systemd 展示白名单；只决定显示什么，不授予操作权限 */
  systemd_units?: string[];
  /** L2 编辑表单用（`?full=1` 才有）：Beszel 已发现的全部 unit 名，供白名单选择器；
   *  Beszel 快照没缓存时为空数组，表单退化成手打 unit 名 */
  discovered_units?: string[];
  /** 服务升级闸（2026-10-22）：enabled=false/缺失 = 本机服务 UI 不出 upgrade 按钮；
   *  proxy_env 可选注入远端 docker compose 的 env（键白名单见后端 machine_config） */
  upgrade?: { enabled: boolean; proxy_env?: Record<string, string> } | null;
}

/**
 * B3 机器编辑表单（§5.3）。全部存字符串：受控 input 不能塞 number|undefined，
 * 否则清空输入框会变成 NaN。数值/嵌套结构在 saveMachine 里转。
 */
export interface MachineFormValues {
  id: string;
  label: string;
  role: 'server' | 'node';
  provider: string;
  os: string;
  region: string;
  public_host: string;
  tailscale: string;
  ssh: string;
  ssh_key: string;
  beszel_name: string;
  capabilities: string[];
  /** tcp 端口；留空 = 不探活 */
  probe_port: string;
  /** 家宽公网探测 tcp 端口；留空 = 不探（必须同时有 public_host） */
  public_probe_port: string;
  traffic_quota_gb: string;
  traffic_nic: string;
  renewal_ref: string;
  /** 逗号/换行分隔的 systemd 展示白名单原文；saveMachine 里拆分 trim 去重后发数组 */
  systemd_units_text: string;
  /** 允许本机服务从 UI 点 upgrade（对应 machines.yaml `upgrade.enabled`） */
  upgrade_enabled: boolean;
  /** 升级时注入远端 docker compose 的 env；一行一个 `KEY=value`，留空不注入 */
  upgrade_proxy_env_text: string;
}

export interface MonitorSystem {
  id: string;
  name: string;
  machine?: string;
  host?: string;
  status?: string;
  cpu?: number;
  /** 展示用：优先 MemAvailable 口径 */
  memory_pct?: number;
  /** Beszel 原始 mp（可能含 page cache） */
  memory_pct_raw?: number;
  /** available = 按 MemAvailable；beszel = 沿用 Hub mp */
  memory_basis?: 'available' | 'beszel';
  disk_pct?: number;
  container_count?: number;
  load_avg?: number[];
  uptime_sec?: number;
  net_bytes?: number;
  temperature?: number;
  /** §4.3 内存总量（MB） */
  memory_total_mb?: number;
  /** §4.3 内存已用（MB，total - available） */
  memory_used_mb?: number;
  /** §4.3 磁盘总量（GB）；Beszel 不提供则永不出现 */
  disk_total_gb?: number;
  /** §4.3 磁盘已用（GB） */
  disk_used_gb?: number;
  /** E1 计费网卡名（排除 tailscale0 等后挑出的） */
  net_nic?: string;
  /** E1 该网卡累计发送/接收字节（单调计数器，仅后端记账用） */
  net_sent_total?: number;
  net_recv_total?: number;
  /** E2 计费网卡的瞬时上下行速率（字节/秒） */
  net_up_bytes_sec?: number;
  net_down_bytes_sec?: number;
}

export interface MonitorContainer {
  system_id: string;
  system_name?: string;
  /** 归一化机器名（台账 id，如 nas/txy/us），与台账 machine 对齐 */
  machine?: string;
  name: string;
  image?: string;
  status?: string;
  health?: string;
  cpu?: number;
  memory_mb?: number;
  net_io?: number | string;
}

export interface MonitorPayload {
  systems?: MonitorSystem[];
  containers?: MonitorContainer[];
  updated_at?: string;
  probe_interval?: number;
  error?: string;
  snapshot?: SnapshotMeta;
  /** B6-3：业务数据源回显（console_pg 分支带 beszel 则无键） */
  source?: string;
}

export interface SnapshotMeta {
  sampledAt: string | null;
  refreshing: boolean;
  error: boolean;
}

export interface OpLog {
  id: number | string;
  started_at?: string;
  host?: string;
  svc?: string;
  action?: string;
  result?: string;
  operator?: string;
}

export interface UpgradePrepChange {
  category: string;
  severity: 'breaking' | 'security';
  excerpt: string;
  line: number;
}

export interface UpgradePrep {
  review: {
    status: 'pending' | 'prepared' | 'deferred' | 'ignored';
    reason: string;
    checks: Record<string, boolean>;
    decided_at?: string | null;
    updated_at?: string | null;
  };
  checks: Array<{ key: string; label: string; done: boolean }>;
  changes: UpgradePrepChange[];
  category: 'breaking' | 'security' | 'feature';
  comparison: string;
  blockers: string[];
  readiness: 'needs_review' | 'preparation_reviewed';
}

export interface VersionDetail {
  local?: string;
  upstream?: string;
  status?: string;
  outdated?: boolean;
  track_upstream?: boolean;
  compare_url?: string;
  release_notes?: string;
  last_checked?: string;
  error?: string;
  changes?: Array<{ sha: string; msg: string }>;
  upgrade_prep?: UpgradePrep;
}

export interface ConsoleConfig {
  beszel_url?: string;
  probe_interval?: number;
}

// ---- 今日关注 ----

export type AttentionSeverity = 'warn' | 'error';

export interface AttentionItem {
  /** 稳定的提醒身份；续费项含条目 ID、到期日和过期/临近阶段。 */
  dismiss_key?: string;
  severity: AttentionSeverity;
  kind: string;
  title: string;
  detail: string;
  link: string;
}

// ---- 运维可见性 ----

/** `/api/ops-status` 的 `last_deploy`：deploy.sh 写的白名单四键，缺文件/坏 JSON 整体为 null；
 * 对象存在时四键各自独立可空（例如迁移未落地时 `migrations_applied` 为 null）。 */
export interface OpsLastDeploy {
  commit: string | null;
  backup_tag: string | null;
  deployed_at: string | null;
  migrations_applied: number | null;
}

/** F17 定时备份状态；`ok` 要求成功标记与引用快照同时存在且 last_success 年龄 ≤26h。 */
export type OpsTimedBackupState = 'ok' | 'stale' | 'failed' | 'unknown' | 'never';

export interface OpsTimedBackup {
  state: OpsTimedBackupState;
  /** 最近一次尝试时刻（含时区 ISO8601）；拿不到为 null */
  last_attempt: string | null;
  /** 最近一次已知成功时刻；failed/unknown/stale 时仍可能带出旧值 */
  last_success: string | null;
  /** 成功标记引用的快照文件名（不含路径） */
  snapshot: string | null;
  /** 最近一次失败阶段（如 quick_check）；成功时为 null */
  stage: string | null;
}

/** 后端七键固定契约（`ops_status.get_ops_status`）：字段永远存在，拿不到就是 null，不猜。 */
export interface OpsStatus {
  running_commit: string | null;
  versions_last_checked: string | null;
  feed_last_fetched: string | null;
  last_backup_tag: string | null;
  last_deploy: OpsLastDeploy | null;
  last_probe_run: string | null;
  timed_backup: OpsTimedBackup | null;
}

// ---- Track J（§4.2 /api/observability）----

export interface ObservabilitySourceError {
  /** 只能是 auth|systems|alerts|alerts_history|systemd_services|smart_devices */
  source: string;
  /** 脱敏摘要，不含 URL/凭据/原始响应 */
  message: string;
}

export interface ObservabilityAlert {
  /** beszel_name → machines.yaml id 映射后的机器 id */
  machine: string;
  name: string;
  severity: 'warn' | 'error' | string;
  threshold: number | null;
  duration_min: number | null;
  triggered_at: string | null;
}

export interface ObservabilityEvent {
  machine: string;
  name: string;
  severity: 'warn' | 'error' | string;
  threshold: number | null;
  opened_at: string;
  resolved_at: string | null;
  /** active | resolved */
  state: string;
  duration_sec: number | null;
}

export interface SystemdService {
  machine: string;
  unit: string;
  /** 0 active / 1 inactive / 2 failed / …（后端已转枚举字符串） */
  state: string;
  sub_state: string;
  cpu_pct: number | null;
  cpu_peak_pct: number | null;
  memory_bytes: number | null;
  memory_peak_bytes: number | null;
  updated_at: string | null;
}

export interface SmartDevice {
  machine: string;
  device: string;
  type: string | null;
  model: string | null;
  /** 原样字符串（trim 后）；UI 只对非空且非 PASSED 判异常 */
  state: string;
  capacity_bytes: number | null;
  /** 0/负数已由后端归一为 null */
  temperature_c: number | null;
  power_on_hours: number | null;
  cycles: number | null;
  updated_at: string | null;
}

export interface ObservabilityPayload {
  ok: boolean;
  source?: 'beszel' | 'console_pg';
  /** 显式能力状态；未提供沿用旧契约，unsupported 不等于实查无异常。 */
  capabilities?: Partial<Record<'systemd' | 'smart', 'supported' | 'partial' | 'unsupported'>>;
  updated_at: string | null;
  probe_interval: number;
  source_errors: ObservabilitySourceError[];
  /** Console 内部 Beszel 深链（/beszel/system/<id>），前端不拼 PocketBase URL */
  machine_links: Record<string, string>;
  active_alerts: ObservabilityAlert[];
  recent_events: ObservabilityEvent[];
  systemd_services: SystemdService[];
  smart_devices: SmartDevice[];
}

// ---- 设备历史（只读 PG 主机标量） ----

export interface MonitorHistorySeries {
  metric: string;
  unit: string;
  interval_sec: number;
  points: Array<{ ts: string; value: number }>;
  gaps: Array<{ start: string; end: string; reason: 'no_samples' | 'retention_exceeded' | 'archived' | 'unsupported' }>;
}

export interface MonitorHistoryPayload {
  ok: boolean;
  machine: string;
  source: 'console_pg';
  resolution: 'raw' | 'hourly';
  window: { start: string; end: string; effective_end: string };
  series: MonitorHistorySeries[];
  events: MonitorEvent[];
  error?: string;
}

// ---- 到期提醒状态（供机器摘要与今日关注消费） ----

export type RenewalStatus = 'active' | 'not_renewing' | 'ended';

// ---- Track C10-a（设置页只读「有效配置」）----

/** 字段来源三态：env > yaml > default，与后端 effective_config 一致。 */
export type EffectiveConfigSource = 'env' | 'yaml' | 'default';

export interface EffectiveConfigField {
  key: string;
  value: string | number;
  source: EffectiveConfigSource;
  /** machines 段的字段带所属机器 id（分组字段不带） */
  machine?: string;
}

export interface EffectiveConfigGroup {
  title: string;
  entries: EffectiveConfigField[];
}

export interface EffectiveConfig {
  /** ISO-8601 UTC（Z 后缀） */
  generated_at: string;
  /** DB `schema_migrations` 已应用的最大迁移版本（2026-09-06 基线为 8）；库缺失 / 未跑过迁移为 null，不回退代码版本 */
  schema_version: number | null;
  /** 复用 ops_status 的注入逻辑（GIT_COMMIT / 本地 .git） */
  running_commit: string | null;
  /** 机器探针守护线程上次运行（ISO UTC）；进程内未跑过为 null */
  last_probe_run: string | null;
  /** 台账逐台 yaml 字段 + env 覆盖行（敏感值已遮蔽为 ***） */
  machines: EffectiveConfigField[];
  groups: Record<string, EffectiveConfigGroup>;
}

// ---- C10-b（设置页写回：settings.yaml 白名单）----

/** 可写项来源：env（.env/compose，UI 只读）> settings（settings.yaml）> default。 */
export type ConsoleSettingSource = 'env' | 'settings' | 'default';

export interface ConsoleSettingEntry {
  key: string;
  title: string;
  description: string;
  /** 当前生效值（字符串形态，后端已归一化） */
  value: string;
  source: ConsoleSettingSource;
  /** false = `.env` / compose 已锁定，UI 只读 */
  writable: boolean;
  /** 只读时给人看的原因；可写为 null */
  readonly_reason: string | null;
  type: 'int' | 'string';
  min: number | null;
  max: number | null;
  unit: string;
  default: string;
  /** true = 改完要重启容器才生效（当前白名单三项都是 false） */
  requires_restart: boolean;
}

export interface ConsoleSettingsGroup {
  title: string;
  entries: ConsoleSettingEntry[];
}

export interface ConsoleTableRows {
  name: string;
  rows: number;
}

export interface ConsoleSettings {
  generated_at: string;
  /** 文件名（不含路径） */
  settings_file: string;
  settings_file_exists: boolean;
  groups: Record<string, ConsoleSettingsGroup>;
  tables: ConsoleTableRows[];
  /** 「安全」分区展示：永不可写的键名/键族（只有名字，永不回显值） */
  never_writable: string[];
  /** PUT 响应才有：本次改动的键 */
  changed?: string[];
  /** PUT 响应才有：顺带清理掉的 oplog 行数 */
  pruned_oplog?: number;
  /** PUT 响应才有：顺带清理掉的已恢复监控事件行数（Track K） */
  pruned_events?: number;
}

// ---- Track W2（C2 Tailscale tailnet 视图，LocalAPI 版）----

export interface TailnetMachine {
  machine_id: string;
  hostname: string | null;
  /** null = 该机器未在 tailnet status 里找到（显式未知，而非离线） */
  online: boolean | null;
  /** null = 不过期/未知；负数 = 已过期天数 */
  key_expiry_days: number | null;
  last_seen: string | null;
  client_version: string | null;
  /** D4：peer OS 文本（linux/windows/…）；null = 未知 */
  os: string | null;
  /** D4：该机 tailscale 有新版本；null = 未知（peer 缺失） */
  update_available: boolean | null;
  /** D4：key 已授权；null = 未知（peer 缺失） */
  authorized: boolean | null;
}

export interface TailnetStatus {
  /** false = 文件过期/损坏或既有 LocalAPI 不可用，显式降级而非报错 */
  available: boolean;
  /** 接口采样时间，不表示文件采集成功。 */
  sampled_at: string | null;
  /** 文件源 mtime；socket/旧后端可为 null/缺省，不能由 sampled_at 补造。 */
  source_sampled_at?: string | null;
  error: string | null;
  machines: TailnetMachine[];
}

/** C11：Caddy admin 导出的单个 upstream（address 不出后端，只回服务名/计数）。 */
export interface EntryTrafficUpstream {
  id: string;
  name: string | null;
  num_requests: number | null;
  fails: number | null;
}

/**
 * C11 累计量一行（域名维度，来自 aly Caddy 的 Prometheus 指标）。
 *
 * 注意与 `EntryTrafficUpstream` **不是同一个维度**：那边是上游地址，这边是域名，
 * 一个域名可能按 path 分流到多个上游，所以两张表行数对不上是正常的。
 */
export interface EntryTrafficHost {
  /** 域名；`_other` = Host 头不匹配任何站点（扫描器/按 IP 直连），或被后端折叠的裸 IP */
  host: string;
  requests_total: number;
  ok_2xx: number;
  redirect_3xx: number;
  client_4xx: number;
  server_5xx: number;
}

export interface EntryTrafficStatus {
  /** false = 上游导出文件缺失/损坏/过期，显式降级而非报错 */
  available: boolean;
  /** 接口采样时间，不表示文件采集成功。 */
  sampled_at: string | null;
  /** upstream JSON 的 mtime，与指标采集独立；旧后端可缺省。 */
  upstreams_sampled_at?: string | null;
  error: string | null;
  upstreams: EntryTrafficUpstream[];
  /**
   * 累计量是**独立数据链**（Prometheus 指标文件），与 upstreams 各自降级：
   * 指标文件坏了 `available` 仍可能是 true，反之亦然。
   */
  metrics_available: boolean;
  metrics_error: string | null;
  /** 指标文件 mtime；旧后端可缺省，不同于 API 读取时间及计数起点。 */
  metrics_sampled_at?: string | null;
  /**
   * 累计的起点（ISO-8601 UTC）。Caddy 的计数器**每次 reload 归零**，所以这不是
   * 「开天辟地以来」，必须连同数字一起显示，否则数值会被严重误读。
   */
  counting_since: string | null;
  hosts: EntryTrafficHost[];
}

// ---- C8 / D1'（总控台布局 override 升库）----

/** dashboard_layout 表一行：widget 的用户自定义（坐标 + 显隐）。 */
export interface DashboardLayoutItem {
  widget_key: string;
  x: number;
  y: number;
  w: number;
  /** true = 该 widget 被隐藏；隐藏行坐标保留（找回后恢复原位） */
  hidden: boolean;
}

/** GET/PUT /api/dashboard-layout 的响应形状；updated_at = 最近一次写入时刻。 */
export interface DashboardLayoutPayload {
  items: DashboardLayoutItem[];
  updated_at: string | null;
}

// ---- Track K（事件时间线）----

export type MonitorEventKind = 'machine' | 'service' | 'public';
export type MonitorEventState = 'active' | 'resolved';

export interface MonitorEvent {
  id: number;
  /** 幂等键 `kind:target`；同一目标的连续失败合成一条事件 */
  fingerprint: string;
  kind: MonitorEventKind;
  /** 产生它的探测器：probe（机器探活）/ health（服务健康）/ public_probe（家宽公网） */
  source: string;
  /** 机器 id 或服务名 */
  target: string;
  /** 归属机器 id；服务事件也有，供筛选与折叠 */
  machine: string;
  severity: 'error' | 'warn';
  title: string;
  detail: string;
  opened_at: string;
  updated_at: string;
  /** null = 尚未恢复（活动事件） */
  resolved_at: string | null;
  state: MonitorEventState;
  /** 已恢复 = 实际持续时长；活动中 = 截至 generated_at 的持续时长 */
  duration_sec: number | null;
  /** 活动机器事件才有：同机活动服务事件条数（K3 影响面） */
  impact_count?: number;
  /** 活动服务事件才有：覆盖它的机器事件 fingerprint */
  collapsed_by?: string | null;
}

export interface MonitorEventPage {
  events: MonitorEvent[];
  total: number;
  limit: number;
  offset: number;
  /** 当前未恢复的事件总数（不受筛选与分页影响） */
  active_count: number;
  window_days: number;
  generated_at: string;
}

export interface MonitorEventQuery {
  days?: number;
  kind?: MonitorEventKind;
  machine?: string;
  state?: MonitorEventState | 'all';
  limit?: number;
  offset?: number;
}
