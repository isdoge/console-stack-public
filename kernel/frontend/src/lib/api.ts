import axios from 'axios';
import type {
  AttentionItem,
  CapacityPayload,
  ConsoleConfig,
  ConsoleSettings,
  DashboardLayoutItem,
  DashboardLayoutPayload,
  EffectiveConfig,
  HealthProbeSpec,
  Machine,
  MachineFormValues,
  MonitorEventPage,
  MonitorEventQuery,
  MonitorPayload,
  MonitorHistoryPayload,
  ObservabilityPayload,
  OpLog,
  OpsLastDeploy,
  OpsStatus,
  OpsTimedBackup,
  OpsTimedBackupState,
  Service,
  SnapshotMeta,
  ServiceFormValues,
  EntryTrafficStatus,
  TailnetStatus,
  UpgradePrep,
  VersionDetail,
} from './types';

export const API_BASE = '/api';

const http = axios.create({ baseURL: API_BASE, timeout: 30000 });

/** 统一从请求错误里取后端 error 字段（鸭子类型兼容 axios 实例与测试桩），兜底 Error.message。 */
export function apiErrorMessage(err: unknown): string {
  const data = (err as { response?: { data?: { error?: string } } } | undefined)?.response?.data;
  if (data?.error) return data.error;
  if (err instanceof Error) return err.message;
  return typeof err === 'string' ? err : '';
}

// 慢接口单独放宽：默认 30s 只为不让页面无限 pending，
// 覆盖值给同步探测 / SSH 动作 / 上游 API 这类后端本身要等外部响应的请求。
const SLOW = {
  probe: 90000,   // saveService 附带 access 同步探测（多目标）
  action: 60000,  // 容器/节点 SSH 动作，后端日志读取自身 timeout 25s
  upgrade: 420000, // compose pull/up 最多 300s，再同步单服务版本
  upstream: 60000, // 版本同步 / feed 强刷要等 GitHub
};

export async function getConfig(): Promise<ConsoleConfig> {
  const res = await http.get<ConsoleConfig>('/config');
  return res.data || {};
}

function snapshotMeta(headers: Record<string, unknown> | undefined): SnapshotMeta {
  const sampledAt = headers?.['x-snapshot-at'];
  return {
    sampledAt: typeof sampledAt === 'string' && sampledAt ? sampledAt : null,
    refreshing: headers?.['x-snapshot-refreshing'] === '1',
    error: headers?.['x-snapshot-error'] === '1',
  };
}

export async function getServicesSnapshot(force = false): Promise<{ services: Service[]; snapshot: SnapshotMeta }> {
  const res = await http.get<Service[]>(`/services${force ? '?force=1' : ''}`);
  return {
    services: Array.isArray(res.data) ? res.data : [],
    snapshot: snapshotMeta(res.headers as Record<string, unknown>),
  };
}

export async function listServices(force = false): Promise<Service[]> {
  return (await getServicesSnapshot(force)).services;
}

export async function listOplogs(q?: string): Promise<OpLog[]> {
  const res = await http.get<OpLog[]>('/oplog', { params: q ? { q } : undefined });
  return Array.isArray(res.data) ? res.data : [];
}

export async function getMonitor(force = false): Promise<MonitorPayload> {
  const res = await http.get<MonitorPayload>(`/monitor${force ? '?force=1' : ''}`);
  return { ...(res.data || {}), snapshot: snapshotMeta(res.headers as Record<string, unknown>) };
}

export async function getMonitorHistory(query: {
  machine: string;
  metrics: string[];
  start: string;
  end: string;
}): Promise<MonitorHistoryPayload> {
  const res = await http.get<MonitorHistoryPayload>('/monitor/history', {
    params: { ...query, metrics: query.metrics.join(','), resolution: 'auto' },
  });
  if (!res.data?.ok || !Array.isArray(res.data.series) || !Array.isArray(res.data.events)) {
    throw new Error(res.data?.error || '历史数据响应无效');
  }
  return res.data;
}

/**
 * M2 磁盘增长趋势（GET /api/capacity）。取数失败后端也回 HTTP 200 + `ok=false`，
 * 因此这里跟 getObservability 一样归一化而不是当异常抛；`machines` 缺失归空对象。
 */
export async function getCapacity(force = false): Promise<CapacityPayload> {
  const res = await http.get<Partial<CapacityPayload>>(`/capacity${force ? '?force=1' : ''}`);
  const data = res.data || {};
  return {
    ok: data.ok ?? true,
    source: data.source,
    ttl: data.ttl ?? 0,
    history_type: data.history_type ?? '',
    min_r_squared: data.min_r_squared ?? null,
    machines: data.machines ?? {},
    error: data.error ?? null,
  };
}

/** 机器台账（§4.2 GET /api/machines）；Track B 未就绪时后端返回 404，调用方需兜底。 */
export async function listMachines(force = false): Promise<Machine[]> {
  const res = await http.get<Machine[]>(`/machines${force ? '?force=1' : ''}`);
  return Array.isArray(res.data) ? res.data : [];
}

/**
 * J（§4.2 GET /api/observability）。四个 collection 任一失败时后端仍回 HTTP 200 + `ok=false`，
 * 因此这里不做「请求失败 = 整体不可用」的假设；数组字段缺失统一归一为空数组。
 */
export async function getObservability(force = false): Promise<ObservabilityPayload> {
  const res = await http.get<Partial<ObservabilityPayload>>(
    `/observability${force ? '?force=1' : ''}`
  );
  const data = res.data || {};
  return {
    ok: data.ok ?? true,
    source: data.source,
    capabilities: data.capabilities,
    updated_at: data.updated_at ?? null,
    probe_interval: data.probe_interval ?? 0,
    source_errors: Array.isArray(data.source_errors) ? data.source_errors : [],
    machine_links: data.machine_links ?? {},
    active_alerts: Array.isArray(data.active_alerts) ? data.active_alerts : [],
    recent_events: Array.isArray(data.recent_events) ? data.recent_events : [],
    systemd_services: Array.isArray(data.systemd_services) ? data.systemd_services : [],
    smart_devices: Array.isArray(data.smart_devices) ? data.smart_devices : [],
  };
}

/**
 * §4.1：systemd 展示白名单文本 → 数组。按逗号（含全角）/换行拆分，trim、去重、保留首次出现顺序。
 * 只做前端清洗，非法字符/长度上限由后端严格校验（J-B）。
 */
export function parseSystemdUnits(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of String(text ?? '').split(/[,，\n]/)) {
    const t = part.trim();
    if (!t || seen.has(t)) continue;
    seen.add(t);
    out.push(t);
  }
  return out;
}

/**
 * 机器 upgrade.proxy_env 文本 → 对象。每行 `KEY=value`，忽略空行与 `#` 开头注释；
 * 键/值的合法性（白名单 + 字符集）由后端严格校验，这里只做形状解析。
 */
export function parseProxyEnvText(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of String(text ?? '').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    const value = line.slice(eq + 1).trim();
    if (!key) continue;
    out[key] = value;
  }
  return out;
}

/** proxy_env 对象 → 文本（编辑表单回填用；键按字典序稳定输出）。 */
export function formatProxyEnvText(env?: Record<string, string> | null): string {
  if (!env) return '';
  return Object.keys(env)
    .sort()
    .map((key) => `${key}=${env[key]}`)
    .join('\n');
}

/**
 * B3：编辑表单用的单条台账。`full=1` 才带 tailscale/ssh/ssh_key——列表接口刻意不给，
 * 不读全量就保存会把这三项清空。ssh_key 只是 `.ssh-console/` 内的文件名，私钥不经 HTTP。
 */
export async function getMachineForEdit(id: string): Promise<Machine> {
  const res = await http.get<Machine>(`/machines/${encodeURIComponent(id)}?full=1`);
  return res.data || ({} as Machine);
}

export interface SaveMachineResult {
  ok?: boolean;
  machine?: Machine;
  error?: string;
}

/** 新增/更新机器台账（写回 machines.yaml）。id 不可改名，后端会拒。 */
export async function saveMachine(
  form: MachineFormValues,
  originalId?: string
): Promise<SaveMachineResult> {
  const payload = {
    id: form.id,
    label: form.label,
    role: form.role,
    provider: form.provider,
    os: form.os,
    region: form.region,
    public_host: form.public_host,
    tailscale: form.tailscale,
    ssh: form.ssh,
    ssh_key: form.ssh_key,
    beszel_name: form.beszel_name,
    capabilities: form.capabilities,
    // 端口留空即不配探活，后端按 null 处理
    probe: form.probe_port ? { type: 'tcp', port: form.probe_port } : null,
    // 家宽公网探测同一形状；每次整份提交，留空即清掉
    public_probe: form.public_probe_port ? { type: 'tcp', port: form.public_probe_port } : null,
    traffic_quota_gb: form.traffic_quota_gb,
    traffic_nic: form.traffic_nic,
    renewal_ref: form.renewal_ref,
    // §4.1：白名单只决定显示什么，不授予操作权限；空值发 []，不丢字段
    systemd_units: parseSystemdUnits(form.systemd_units_text),
    // 升级闸：未勾且未填代理 → null（不写回）；任一非空就发对象
    // ?? '' 兜底：调用方可能拿旧形状的对象直接调 saveMachine（如旧测试 fixture）
    upgrade: (form.upgrade_enabled || (form.upgrade_proxy_env_text ?? '').trim())
      ? (() => {
          const proxyEnv = parseProxyEnvText(form.upgrade_proxy_env_text ?? '');
          return {
            enabled: form.upgrade_enabled === true,
            ...(Object.keys(proxyEnv).length > 0 ? { proxy_env: proxyEnv } : {}),
          };
        })()
      : null,
  };
  const res = originalId
    ? await http.put<SaveMachineResult>(`/machines/${encodeURIComponent(originalId)}`, payload)
    : await http.post<SaveMachineResult>('/machines', payload);
  return res.data || {};
}

/** 删除机器台账。机器上还有服务时后端以中文 400 拒绝（§5.3 删除防呆）。 */
export async function deleteMachine(id: string): Promise<void> {
  await http.delete(`/machines/${encodeURIComponent(id)}`);
}

/** E3：node 白名单动作（当前只有 restart-singbox）。命令写死在后端，这里只送 key。 */
export async function runMachineAction(
  machine: string,
  action: string
): Promise<ServiceActionResult> {
  const res = await http.post<ServiceActionResult>(
    '/machine/action',
    { machine, action },
    { timeout: SLOW.action }
  );
  return res.data || {};
}

export interface ServiceActionResult {
  ok?: boolean;
  output?: string;
  error?: string;
  warning?: string;
}

export async function runServiceAction(name: string, action: string): Promise<ServiceActionResult> {
  const res = await http.post<ServiceActionResult>(
    '/service/action', { name, action }, { timeout: action === 'upgrade' ? SLOW.upgrade : SLOW.action }
  );
  return res.data || {};
}

export interface SaveServiceResult {
  ok?: boolean;
  access_probe?: Record<string, { configured: boolean; ok: boolean | null; msg?: string }>;
  error?: string;
}

export async function saveService(
  form: ServiceFormValues,
  originalName?: string
): Promise<SaveServiceResult> {
  const res = await http.post<SaveServiceResult>(
    '/service',
    {
      ...form,
      _original_name: originalName,
      access: { home: form.home, tailscale: form.tailscale, domain: form.domain },
      version: { repo_url: form.repo_url, enabled: form.version_enabled, track_upstream: form.track_upstream },
      health_probe: healthProbePayload(form),
    },
    { timeout: SLOW.probe }
  );
  return res.data || {};
}

/** L1 三态 → 后端 `health_probe`：关闭发 false、默认发 true（后端据此删键）、
 *  语义检查发对象。空串字段一律省略 = 不校验该条。 */
export function healthProbePayload(form: ServiceFormValues): boolean | HealthProbeSpec {
  if (form.health_probe_mode === 'off') return false;
  if (form.health_probe_mode !== 'http') return true;
  const spec: HealthProbeSpec = { type: 'http', target: form.health_probe_target };
  // 留空要发**显式 null**，不能省略：后端对缺键按默认 200 校验（`{type: http}`
  // 这种最小配置总得查点什么），省略掉就把「不校验」静默变成「查 200」了。
  const status = form.health_probe_status.trim();
  spec.expected_status = status === '' ? null : Number(status);
  const latency = form.health_probe_latency.trim();
  if (latency !== '') spec.max_latency_ms = Number(latency);
  const body = form.health_probe_body.trim();
  if (body !== '') spec.body_contains = body;
  return spec;
}

export async function deleteService(name: string): Promise<void> {
  await http.delete(`/service/${encodeURIComponent(name)}`);
}

// ---- 服务发现（只读 docker ps 白名单扫描 + 勾选批量登记） ----

export interface DiscoveredContainer {
  name: string;
  image: string;
  status: string;
  /** L4：Docker 官方 compose project label；空串 = 未分组（手工 docker run 起的） */
  stack?: string;
}

export interface DiscoverServicesResult {
  ok?: boolean;
  machine?: string;
  total?: number;
  registered?: number;
  candidates?: DiscoveredContainer[];
  error?: string;
}

/** 只读扫描：后端跑写死的 `docker ps -a`，对照台账返回未登记容器。 */
export async function discoverMachineServices(
  machineId: string
): Promise<DiscoverServicesResult> {
  const res = await http.get<DiscoverServicesResult>(
    `/machines/${encodeURIComponent(machineId)}/discover`,
    { timeout: SLOW.action }
  );
  return res.data || {};
}

export interface DiscoveredRegistration {
  container: string;
  /** 登记名，缺省用容器名 */
  name?: string;
  /** 健康检查端口；缺省不配探活 */
  port?: number | null;
}

/** 逐个走既有 POST /api/service 登记（复用其校验与 oplog），汇总成败。 */
export async function registerDiscoveredServices(
  machine: string,
  items: DiscoveredRegistration[]
): Promise<{ added: string[]; failed: { name: string; error: string }[] }> {
  const added: string[] = [];
  const failed: { name: string; error: string }[] = [];
  for (const item of items) {
    const name = item.name || item.container;
    try {
      const res = await http.post<SaveServiceResult>(
        '/service',
        {
          name,
          machine,
          type: 'docker',
          container: item.container,
          port: item.port || null,
          purpose: 'docker ps 扫描登记',
          health_probe: item.port ? undefined : false,
        },
        { timeout: SLOW.probe }
      );
      if (res.data?.ok === false) failed.push({ name, error: res.data.error || '未知错误' });
      else added.push(name);
    } catch (err) {
      failed.push({ name, error: apiErrorMessage(err) });
    }
  }
  return { added, failed };
}

export async function setServicesPriority(names: string[], priority: string): Promise<void> {
  await http.post('/services/priority', { names, priority });
}

export interface SuggestAccessResult {
  home?: string;
  tailscale?: string;
}

export async function suggestAccess(
  machine: string,
  port: string,
  pathSuffix: string
): Promise<SuggestAccessResult> {
  const res = await http.get<SuggestAccessResult>('/service/suggest-access', {
    params: { machine, port, path_suffix: pathSuffix },
  });
  return res.data || {};
}

export async function getVersionDetail(name: string, sync = false): Promise<VersionDetail> {
  const res = await http.get<VersionDetail>(
    `/service/${encodeURIComponent(name)}/version${sync ? '?sync=1' : ''}`,
    { timeout: sync ? SLOW.upstream : undefined }
  );
  return res.data || {};
}

export async function saveUpgradeReview(
  name: string,
  update: { status?: string; reason?: string; checks?: Record<string, boolean> },
): Promise<UpgradePrep['review']> {
  const res = await http.post<{ ok?: boolean; review?: UpgradePrep['review']; error?: string }>(
    `/service/${encodeURIComponent(name)}/upgrade-review`, update);
  if (!res.data?.review) throw new Error(res.data?.error || '保存失败');
  return res.data.review;
}

export function extractPathSuffix(url?: string): string {
  if (!url) return '';
  try {
    const u = new URL(url);
    return `${u.pathname === '/' ? '' : u.pathname}${u.hash || ''}`;
  } catch {
    return '';
  }
}

export async function getAttention(): Promise<AttentionItem[]> {
  const res = await http.get<AttentionItem[]>('/attention');
  return Array.isArray(res.data) ? res.data : [];
}

/** `last_deploy` 逐键归一：对象不存在/非对象整体 null；键存在时用 `??` 而非 `||`，
 * 避免把合法的 `migrations_applied: 0` 当缺失丢弃。 */
function normalizeLastDeploy(raw: unknown): OpsLastDeploy | null {
  if (!raw || typeof raw !== 'object') return null;
  const value = raw as Partial<OpsLastDeploy>;
  return {
    commit: value.commit ?? null,
    backup_tag: value.backup_tag ?? null,
    deployed_at: value.deployed_at ?? null,
    migrations_applied: value.migrations_applied ?? null,
  };
}

const TIMED_BACKUP_STATES: readonly OpsTimedBackupState[] = [
  'ok',
  'stale',
  'failed',
  'unknown',
  'never',
];

/** F17 `timed_backup` 逐键归一：对象不存在/非对象整体 null（旧后端无此键）；
 * state 不在枚举内归为 `unknown`，绝不把未知状态当成 `ok`。 */
function normalizeTimedBackup(raw: unknown): OpsTimedBackup | null {
  if (!raw || typeof raw !== 'object') return null;
  const value = raw as Partial<OpsTimedBackup>;
  const state = TIMED_BACKUP_STATES.includes(value.state as OpsTimedBackupState)
    ? (value.state as OpsTimedBackupState)
    : 'unknown';
  return {
    state,
    last_attempt: value.last_attempt ?? null,
    last_success: value.last_success ?? null,
    snapshot: value.snapshot ?? null,
    stage: value.stage ?? null,
  };
}

export async function getOpsStatus(): Promise<OpsStatus> {
  const res = await http.get<OpsStatus>('/ops-status');
  const data = res.data;
  return {
    running_commit: data?.running_commit ?? null,
    versions_last_checked: data?.versions_last_checked ?? null,
    feed_last_fetched: data?.feed_last_fetched ?? null,
    last_backup_tag: data?.last_backup_tag ?? null,
    // 旧四键部署（无这两个字段）归一为 null，保持向后兼容
    last_deploy: normalizeLastDeploy(data?.last_deploy),
    last_probe_run: data?.last_probe_run ?? null,
    // F17：旧后端无此键 → null，卡片显示未知而不是伪装正常
    timed_backup: normalizeTimedBackup(data?.timed_backup),
  };
}

// ---- C1 一键巡检报告 ----

/** 后端 _summary 的判定：三档，不在前端重算。 */
export type InspectionVerdict = '正常' | '留意' | '需处理';

export interface InspectionSummary {
  verdict: InspectionVerdict;
  attention_error: number;
  attention_warn: number;
  machines_total: number;
  machines_down: string[];
  services_total: number;
  services_unhealthy: string[];
  renewals_total: number;
  renewals_expired: string[];
  renewals_due_7d: string[];
  /** 取数失败的段名；非空即该段内容不可信 */
  sections_degraded: string[];
}

/**
 * 每段固定带 `sampled_at`：**该段最旧的采样时刻**（UTC），不是报告生成时间——
 * 关注项与监控事件读的是只读缓存，拿生成时间冒充会把陈旧数据说成当前状态。
 * 段内其余字段各不相同（items / status / active_alerts …），故留索引签名。
 */
export interface InspectionSection<T = unknown> {
  sampled_at: string | null;
  error: string | null;
  items?: T[];
  [key: string]: unknown;
}

export interface InspectionReport {
  generated_at: string;
  summary: InspectionSummary;
  sections: Record<string, InspectionSection>;
}

/** JSON 形态，给 agent / 后续页内消费。 */
export async function getInspectionReport(): Promise<InspectionReport> {
  const res = await http.get<InspectionReport>('/inspection/report', {
    params: { format: 'json' },
  });
  return res.data;
}

/**
 * markdown 形态，给人贴回 daily。
 * 后端回 `text/markdown`，axios 默认 transformResponse 会先试 JSON.parse；
 * 报告正文以 `#` 开头虽然解析必然失败，但显式关掉转换才不依赖那个巧合。
 */
export async function getInspectionReportMarkdown(): Promise<string> {
  const res = await http.get<string>('/inspection/report', {
    params: { format: 'markdown' },
    responseType: 'text',
    transformResponse: [(data) => data],
  });
  return typeof res.data === 'string' ? res.data : String(res.data ?? '');
}

// ---- Track C10-a（设置页只读「有效配置」）----

/** 只读端点：值 + 来源徽章 + schema/commit/探针时间；敏感值后端已遮蔽为 ***。 */
export async function getEffectiveConfig(): Promise<EffectiveConfig> {
  const res = await http.get<EffectiveConfig>('/config/effective');
  const data = res.data;
  return {
    generated_at: data?.generated_at ?? '',
    // null 是有语义的（DB 侧拿不到已应用版本），不能用 0 冒充
    schema_version: typeof data?.schema_version === 'number' ? data.schema_version : null,
    running_commit: data?.running_commit ?? null,
    last_probe_run: data?.last_probe_run ?? null,
    machines: Array.isArray(data?.machines) ? data.machines : [],
    groups: data?.groups ?? {},
  };
}

// ---- C10-b（设置页写回：白名单键 → settings.yaml）----

function normalizeSettings(data: ConsoleSettings | undefined): ConsoleSettings {
  return {
    generated_at: data?.generated_at ?? '',
    settings_file: data?.settings_file ?? 'settings.yaml',
    settings_file_exists: data?.settings_file_exists === true,
    groups: data?.groups ?? {},
    tables: Array.isArray(data?.tables) ? data.tables : [],
    never_writable: Array.isArray(data?.never_writable) ? data.never_writable : [],
    ...(Array.isArray(data?.changed) ? { changed: data.changed } : {}),
    ...(typeof data?.pruned_oplog === 'number' ? { pruned_oplog: data.pruned_oplog } : {}),
  };
}

/** 可写项 + 各表行数 + 永不可写清单。白名单里没有 secrets，响应不含任何凭据。 */
export async function getConsoleSettings(): Promise<ConsoleSettings> {
  const res = await http.get<ConsoleSettings>('/config/settings');
  return normalizeSettings(res.data);
}

/**
 * 增量写回（只带要改的键）。空串 = 恢复默认。
 * 拒绝三态由后端给状态码：403 永不可写 / 400 不在白名单或值非法 / 409 `.env` 已锁定，
 * 文案经 `apiErrorMessage` 原样呈现给用户。
 */
export async function saveConsoleSettings(
  values: Record<string, string>
): Promise<ConsoleSettings> {
  const res = await http.put<ConsoleSettings>('/config/settings', { values });
  return normalizeSettings(res.data);
}

// ---- Track W2（C2 tailnet 视图）----

export async function getTailnet(): Promise<TailnetStatus> {
  const res = await http.get<TailnetStatus>('/tailnet');
  const data = res.data;
  const machines = Array.isArray(data?.machines) ? data.machines : [];
  return {
    available: data?.available === true,
    sampled_at: data?.sampled_at ?? null,
    source_sampled_at: typeof data?.source_sampled_at === 'string' ? data.source_sampled_at : null,
    error: typeof data?.error === 'string' ? data.error : null,
    machines: machines.map((m) => ({
      machine_id: String(m?.machine_id ?? ''),
      hostname: m?.hostname ?? null,
      online: typeof m?.online === 'boolean' ? m.online : null,
      key_expiry_days: typeof m?.key_expiry_days === 'number' ? m.key_expiry_days : null,
      last_seen: m?.last_seen ?? null,
      client_version: m?.client_version ?? null,
      // D4 三字段（os/update_available/authorized）：非预期类型一律 null（未知）
      os: typeof m?.os === 'string' && m.os.trim() !== '' ? m.os : null,
      update_available: typeof m?.update_available === 'boolean' ? m.update_available : null,
      authorized: typeof m?.authorized === 'boolean' ? m.authorized : null,
    })),
  };
}

export async function getEntryTraffic(): Promise<EntryTrafficStatus> {
  const res = await http.get<EntryTrafficStatus>('/caddy/entry');
  const data = res.data;
  const upstreams = Array.isArray(data?.upstreams) ? data.upstreams : [];
  // 累计量是独立数据链，缺字段/类型不对时单独降级，不牵连 upstreams 那半边。
  const hosts = Array.isArray(data?.hosts) ? data.hosts : [];
  return {
    available: data?.available === true,
    sampled_at: typeof data?.sampled_at === 'string' ? data.sampled_at : null,
    upstreams_sampled_at:
      typeof data?.upstreams_sampled_at === 'string' ? data.upstreams_sampled_at : null,
    error: typeof data?.error === 'string' ? data.error : null,
    upstreams,
    metrics_available: data?.metrics_available === true,
    metrics_error: typeof data?.metrics_error === 'string' ? data.metrics_error : null,
    metrics_sampled_at: typeof data?.metrics_sampled_at === 'string' ? data.metrics_sampled_at : null,
    counting_since:
      typeof data?.counting_since === 'string' ? data.counting_since : null,
    hosts,
  };
}

// ---- C8 / D1'（总控台布局 override 升库）----

export async function getDashboardLayout(): Promise<DashboardLayoutPayload> {
  const res = await http.get<DashboardLayoutPayload>('/dashboard-layout');
  const data = res.data;
  const items = Array.isArray(data?.items) ? data.items : [];
  return {
    // 坐标是后端校验过的整数，这里只做形状兜底；widget_key 空串行丢弃
    items: items
      .map((it) => ({
        widget_key: String(it?.widget_key ?? ''),
        x: Number(it?.x ?? 0),
        y: Number(it?.y ?? 0),
        w: Number(it?.w ?? 12),
        hidden: it?.hidden === true,
      }))
      .filter((it) => it.widget_key !== ''),
    updated_at: typeof data?.updated_at === 'string' ? data.updated_at : null,
  };
}

export async function saveDashboardLayout(
  items: DashboardLayoutItem[]
): Promise<DashboardLayoutPayload> {
  const res = await http.put<DashboardLayoutPayload>('/dashboard-layout', { items });
  return res.data;
}

// ---- Track K（事件时间线）----

/** 事件列表；后端对非法参数一律退化为默认值，不会 400。 */
export async function listMonitorEvents(query: MonitorEventQuery = {}): Promise<MonitorEventPage> {
  const params: Record<string, string> = {};
  if (query.days != null) params.days = String(query.days);
  if (query.kind) params.kind = query.kind;
  if (query.machine) params.machine = query.machine;
  if (query.state && query.state !== 'all') params.state = query.state;
  if (query.limit != null) params.limit = String(query.limit);
  if (query.offset != null) params.offset = String(query.offset);
  const res = await http.get<MonitorEventPage>('/events', { params });
  const data = res.data;
  return {
    events: Array.isArray(data?.events) ? data.events : [],
    total: typeof data?.total === 'number' ? data.total : 0,
    limit: typeof data?.limit === 'number' ? data.limit : 100,
    offset: typeof data?.offset === 'number' ? data.offset : 0,
    active_count: typeof data?.active_count === 'number' ? data.active_count : 0,
    window_days: typeof data?.window_days === 'number' ? data.window_days : 7,
    generated_at: typeof data?.generated_at === 'string' ? data.generated_at : '',
  };
}
