import { useMemo } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowUpRight,
  CalendarClock,
  CheckCircle2,
  RefreshCw,
} from 'lucide-react';
import { priceText, projectionText } from '@/components/monitor/machine-card';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DisplayTime } from '@/components/ui/display-time';
import { isActiveRenewal } from '@/lib/renewal-status';
import type {
  AttentionItem,
  AttentionSeverity,
  Machine,
  MonitorSystem,
  ObservabilityPayload,
  Service,
} from '@/lib/types';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// B1 冻结契约类型与常量（B1-1 provider/DTO + B1-2 新鲜度与五态质量枚举）
// ---------------------------------------------------------------------------

/** B1-1 §5.1：最后已知运行状态（允许过期，来源失败时不自动变 unknown） */
export type LastKnownState = 'up' | 'down' | 'unknown';

/** B1-2 §3.1 / §6：五态展示枚举（优先级：source-failed > never-collected > stale > partial > fresh） */
export type DataQualityState =
  | 'fresh'
  | 'stale'
  | 'source-failed'
  | 'partial'
  | 'never-collected';

/** B1-1 §4.4 / B1-4 §3.3：能力声明状态 */
export type CapabilityStatus = 'supported' | 'partial' | 'unsupported';

export interface CapabilityEntry {
  status: CapabilityStatus;
  /** 机器可读限定说明，如 'mdraid-no-temperature'，不直接裸露为异常文案 */
  note?: string;
}

/**
 * 对现有 MonitorSystem 的向后兼容超集扩展（对齐 B1-1 §4.1 MachineSnapshot）。
 * 新旧字段并存：后端未完成 B4 切换前也能无缝消费现有 `/api/monitor` 载荷。
 */
export interface TodayFocusSystem extends MonitorSystem {
  machine_id?: string;
  last_known_state?: LastKnownState;
  data_quality?: DataQualityState;
  source?: 'beszel' | 'go-agent' | 'ssh-fallback';
  sampled_at?: string | null;
  sample_interval_sec?: number | null;
  agent_version?: string | null;
  agent_update_available?: boolean | null;
  capabilities?: Record<string, CapabilityEntry>;
  missing_fields?: string[];
}

/** 今日关注三大分组键：故障置顶 → 到期居中 → 更新置底 */
export type TodayFocusGroupKey = 'fault' | 'expiring' | 'update';

export const TODAY_FOCUS_GROUP_ORDER: readonly TodayFocusGroupKey[] = [
  'fault',
  'expiring',
  'update',
] as const;

/** B1-2 §4.2 / §6：五态展示文案与视觉分级 */
export const DATA_QUALITY_META: Record<
  DataQualityState,
  {
    label: string;
    badgeClass: string;
    /** B1-2 §3.1 优先级权重：数值越大越优先呈现 */
    priority: number;
  }
> = {
  'source-failed': {
    label: '来源失败',
    badgeClass: 'bg-destructive/15 text-destructive border-destructive/30',
    priority: 400,
  },
  'never-collected': {
    label: '暂无数据',
    badgeClass: 'bg-muted text-muted-foreground border-border/60',
    priority: 300,
  },
  stale: {
    label: '数据过期',
    badgeClass: 'bg-amber-500/15 text-amber-700 dark:text-amber-400 border-amber-500/30',
    priority: 200,
  },
  partial: {
    label: '部分指标缺失',
    badgeClass: 'bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/20',
    priority: 100,
  },
  fresh: {
    label: '实时',
    badgeClass: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/20',
    priority: 0,
  },
};

export interface TodayFocusGroupMeta {
  key: TodayFocusGroupKey;
  title: string;
  description: string;
  /** 点击组头部筛选徽章时的默认直达链接 */
  filterHref: string;
  badgeClass: string;
}

export const TODAY_FOCUS_GROUPS: Record<TodayFocusGroupKey, TodayFocusGroupMeta> = {
  fault: {
    key: 'fault',
    title: '故障与数据异常',
    description: '来源失败、数据过期、从未采到或运行异常',
    filterHref: '/devices?focus=fault',
    badgeClass: 'bg-destructive/15 text-destructive',
  },
  expiring: {
    key: 'expiring',
    title: '即将到期与配额',
    description: '续费、证书、Tailnet 密钥及月流量配额预警',
    filterHref: '/devices?focus=expiring',
    badgeClass: 'bg-amber-500/15 text-amber-700 dark:text-amber-400',
  },
  update: {
    key: 'update',
    title: '待更新提示',
    description: '采集 Agent 或登记服务存在可用更新（非故障）',
    filterHref: '/devices?filter=outdated&focus=update',
    // B1-1 §5.4：更新提示与故障视觉严格分离，使用中性信息色
    badgeClass: 'bg-muted text-muted-foreground',
  },
};

// ---------------------------------------------------------------------------
// 能力判定、缺失表达与质量状态推导（B1-1 §4.4, §5, §6 & B1-2 §3, §4）
// ---------------------------------------------------------------------------

/** 归一获取跨源稳定主键 `machine_id`（B1-1 §2.1：优先 `machine_id`，兼容旧 `machine`） */
export function resolveMachineId(system?: TodayFocusSystem | null, machine?: Machine | null): string {
  return (
    system?.machine_id?.trim().toLowerCase() ||
    system?.machine?.trim().toLowerCase() ||
    machine?.id?.trim().toLowerCase() ||
    ''
  );
}

/**
 * 判定机器对某项能力的支持状态（B1-1 §4.4）。
 * - 优先使用 DTO 显式下发的 `system.capabilities[capabilityKey]`
 * - 过渡期未下发时，按 B1-1 §4.4 静态能力表（平台事实 + Agent 版本）推导
 */
export function resolveCapabilityStatus(
  capabilityKey: 'base' | 'containers' | 'temperature' | 'systemd' | 'package-updates' | 'smart' | 'traffic-ledger',
  machine?: Machine | null,
  system?: TodayFocusSystem | null,
): CapabilityEntry {
  const explicit = system?.capabilities?.[capabilityKey];
  if (explicit) return explicit;

  const machineId = resolveMachineId(system, machine);
  const role = machine?.role;
  const agentVersion = system?.agent_version ?? null;

  switch (capabilityKey) {
    case 'containers':
      if (role === 'node' || machineId === 'kr' || machineId === 'sg') {
        return { status: 'unsupported' };
      }
      return { status: 'supported' };
    case 'temperature':
      // B1-1 §4.4 / §6 规则 3：仅 NAS 有真实传感器读数；其余 dt=0 是无传感器哨兵
      if (machineId && machineId !== 'nas') {
        return { status: 'unsupported' };
      }
      return { status: 'supported' };
    case 'smart':
      if (machineId === 'nas') {
        return { status: 'partial', note: 'mdraid-no-temperature' };
      }
      return { status: 'unsupported' };
    case 'systemd':
    case 'package-updates':
      if (
        agentVersion === '0.19.0' ||
        machineId === 'nas' ||
        machineId === 'txy' ||
        machineId === 'aly'
      ) {
        return { status: 'unsupported' };
      }
      return { status: 'supported' };
    case 'base':
    case 'traffic-ledger':
    default:
      return { status: 'supported' };
  }
}

/**
 * 读取温度值并执行 B1-1 §7 行为修正 A：
 * 无温度传感器机器（或 capability=unsupported，或非 nas 的 dt=0 哨兵）一律归一为 null，禁止显示 0°C。
 */
export function normalizeTemperatureReading(
  machine?: Machine | null,
  system?: TodayFocusSystem | null,
): number | null {
  const cap = resolveCapabilityStatus('temperature', machine, system);
  if (cap.status === 'unsupported') return null;
  const raw = system?.temperature;
  if (raw == null || !Number.isFinite(raw)) return null;
  const machineId = resolveMachineId(system, machine);
  if (machineId !== 'nas' && raw === 0) return null;
  return raw;
}

export interface FormattedReading {
  text: string;
  state: 'valid' | 'missing' | 'unsupported';
  hint?: string;
}

/**
 * B1-1 §6 / B1-2 §4.1：格式化单字段读数，严禁使用 `?? 0` 兜底。
 * - 能力缺失 → `不支持`（灰显，不计入异常与分母）
 * - 数据缺失（null/undefined）→ `—`（灰显横线，提示「暂缺」）
 * - 合法数值（含合法 0）→ 原样格式化
 */
export function formatMetricReading(
  value: number | null | undefined,
  unit: string,
  capability: CapabilityEntry = { status: 'supported' },
  fractionDigits = 0,
): FormattedReading {
  if (capability.status === 'unsupported') {
    return { text: '不支持', state: 'unsupported', hint: '本机平台不支持该指标' };
  }
  if (value == null || !Number.isFinite(value)) {
    return { text: '—', state: 'missing', hint: '暂缺（本轮未采到有效值）' };
  }
  const formatted = fractionDigits > 0 ? value.toFixed(fractionDigits) : String(Math.round(value));
  return {
    text: `${formatted}${unit}`,
    state: 'valid',
    hint: capability.status === 'partial' && capability.note ? `部分支持（${capability.note}）` : undefined,
  };
}

/** B1-1 §5.1：解析最后已知运行状态；来源失败时保留旧快照中的 up/down，不抹成 unknown */
export function resolveLastKnownState(
  machine?: Machine | null,
  system?: TodayFocusSystem | null,
): LastKnownState {
  if (system?.last_known_state === 'up' || system?.last_known_state === 'down' || system?.last_known_state === 'unknown') {
    return system.last_known_state;
  }
  const raw = machine?.role === 'node' ? machine.probe_status : system?.status;
  if (raw === 'up') return 'up';
  if (raw === 'down') return 'down';
  return 'unknown';
}

/**
 * B1-2 §3.1：解析机器的数据质量状态。
 * 前端优先直接消费后端已判定的 `system.data_quality`（不自行用本地时钟重算 12 分钟阈值）；
 * 仅在快照级来源失败（`sourceFailed`）或旧接口尚未提供 `data_quality` 字段时按契约优先级归类。
 */
export function resolveDataQuality(
  machine?: Machine | null,
  system?: TodayFocusSystem | null,
  options?: { sourceFailed?: boolean },
): DataQualityState {
  if (system?.data_quality === 'source-failed') return 'source-failed';
  if (options?.sourceFailed) {
    return system ? 'source-failed' : 'never-collected';
  }
  if (system?.data_quality) {
    return system.data_quality;
  }
  if (!system) {
    return 'never-collected';
  }
  if (Array.isArray(system.missing_fields) && system.missing_fields.length > 0) {
    return 'partial';
  }
  const hasMissingBase =
    system.cpu == null || system.memory_pct == null || system.disk_pct == null;
  if (hasMissingBase) {
    return 'partial';
  }
  return 'fresh';
}

// ---------------------------------------------------------------------------
// 统计分母与数据覆盖计算（必做清单 3：B1-1 §8.3 明确分母，不用全量 6 台冒充）
// ---------------------------------------------------------------------------

export type CoverageMetricKey =
  | 'cpu'
  | 'memory_pct'
  | 'disk_pct'
  | 'temperature'
  | 'container_count';

export interface MetricCoverageStat {
  metric: CoverageMetricKey;
  label: string;
  unit: string;
  /** 台账登记总机器数 */
  registeredTotal: number;
  /** 具备该指标能力的机器数（排除 unsupported） */
  supportedCount: number;
  /** 能力缺失（unsupported）的机器数 */
  unsupportedCount: number;
  /** 统计分母：当前具有有效数值读数的机器数（绝不用全量机器数冒充） */
  validCount: number;
  /** 支持该能力但当前读数缺失（null/—）的机器数 */
  missingCount: number;
  /** 基于 validCount 分母计算的均值；validCount=0 时为 null（不显示 0） */
  average: number | null;
  /** 基于 validCount 分母计算的最大值；validCount=0 时为 null */
  max: number | null;
}

export interface FleetCoverageSummary {
  registeredTotal: number;
  /** 数据质量为 fresh 的机器数 */
  freshCount: number;
  /** 存在有效采样快照（非 never-collected）的机器数 */
  sampledCount: number;
  metrics: Record<CoverageMetricKey, MetricCoverageStat>;
}

/**
 * B1-1 §8.3：计算各关键指标的真实有效读数分母与覆盖情况。
 * - 分母 = `validCount`（实际有该指标有效读数的机器数），严禁用 `registeredTotal`（如 6 台）冒充分母
 * - `unsupported` 机器（如 kr/sg 无容器、非 NAS 无温度）单独计入 `unsupportedCount`，不混入缺失或分母
 */
export function computeMetricCoverage(
  machines: Machine[],
  systems: TodayFocusSystem[],
  options?: { sourceFailed?: boolean },
): FleetCoverageSummary {
  const systemByMachine = new Map<string, TodayFocusSystem>();
  for (const sys of systems) {
    const id = resolveMachineId(sys);
    if (id) systemByMachine.set(id, sys);
  }

  // 若台账尚未加载，按 systems 列表推导候选机器集合
  const targets: Array<{ machine?: Machine; system?: TodayFocusSystem }> =
    machines.length > 0
      ? machines.map((m) => ({ machine: m, system: systemByMachine.get(m.id.toLowerCase()) }))
      : systems.map((s) => ({ system: s }));

  const registeredTotal = targets.length;
  let freshCount = 0;
  let sampledCount = 0;

  for (const { machine, system } of targets) {
    const quality = resolveDataQuality(machine, system, options);
    if (quality === 'fresh') freshCount += 1;
    if (quality !== 'never-collected') sampledCount += 1;
  }

  const buildStat = (
    metric: CoverageMetricKey,
    label: string,
    unit: string,
    capabilityKey: 'base' | 'containers' | 'temperature',
    getter: (m?: Machine, s?: TodayFocusSystem) => number | null | undefined,
  ): MetricCoverageStat => {
    let supportedCount = 0;
    let unsupportedCount = 0;
    let validCount = 0;
    let missingCount = 0;
    let sum = 0;
    let max: number | null = null;

    for (const { machine, system } of targets) {
      const cap = resolveCapabilityStatus(capabilityKey, machine, system);
      if (cap.status === 'unsupported') {
        unsupportedCount += 1;
        continue;
      }
      supportedCount += 1;
      const val = getter(machine, system);
      if (val != null && Number.isFinite(val)) {
        validCount += 1;
        sum += val;
        max = max == null ? val : Math.max(max, val);
      } else {
        missingCount += 1;
      }
    }

    return {
      metric,
      label,
      unit,
      registeredTotal,
      supportedCount,
      unsupportedCount,
      validCount,
      missingCount,
      average: validCount > 0 ? sum / validCount : null,
      max,
    };
  };

  return {
    registeredTotal,
    freshCount,
    sampledCount,
    metrics: {
      cpu: buildStat('cpu', 'CPU', '%', 'base', (_m, s) => s?.cpu),
      memory_pct: buildStat('memory_pct', '内存', '%', 'base', (_m, s) => s?.memory_pct),
      disk_pct: buildStat('disk_pct', '磁盘', '%', 'base', (_m, s) => s?.disk_pct),
      temperature: buildStat('temperature', '温度', '°C', 'temperature', (m, s) =>
        normalizeTemperatureReading(m, s),
      ),
      container_count: buildStat('container_count', '容器', '个', 'containers', (_m, s) =>
        s?.container_count,
      ),
    },
  };
}

// ---------------------------------------------------------------------------
// 今日关注条目模型与分组排序算法（必做清单 1 & 2）
// ---------------------------------------------------------------------------

export interface TodayFocusItem {
  id: string;
  group: TodayFocusGroupKey;
  machineId?: string;
  machineLabel?: string;
  /** 视觉级别：更新组恒为 'info'，与故障/告警严格分离（B1-1 §5.4） */
  severity: AttentionSeverity | 'info';
  /** 组内排序权重（越大越靠前） */
  severityRank: number;
  /** B1-2 五态枚举（仅故障组机器质量条目携带） */
  dataQuality?: DataQualityState;
  /** B1-1 最后已知运行状态 */
  lastKnownState?: LastKnownState;
  /** 徽章短文案，如「数据过期」「来源失败」「暂无数据」「续费」「流量」「Agent 更新」 */
  badge: string;
  title: string;
  detail: string;
  /** 源采样时间或事件触发时间（ISO Z），供排序与 DisplayTime 展示 */
  timestamp?: string | null;
  /** 点击直达路由 */
  href: string;
  /** 机器关键读数快照（缺失显示 —，不支持显示 不支持，不显示 0） */
  readings?: Array<{
    label: string;
    reading: FormattedReading;
  }>;
}

function parseTimestampMs(iso?: string | null): number {
  if (!iso) return -1;
  const normalized = iso.includes('T') ? iso : iso.replace(' ', 'T');
  const withZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? normalized : `${normalized}Z`;
  const ms = Date.parse(withZone);
  return Number.isFinite(ms) ? ms : -1;
}

/**
 * 组内排序比较器（必做清单 2）：
 * 1. 按严重程度权重 `severityRank` 降序（严重问题优先）
 * 2. 同权重按时间戳 `timestamp` 倒序（最新发生/采样优先；无时间戳排后）
 * 3. 稳定兜底：按 `machineId` 与 `id` 字典序
 */
export function compareTodayFocusItems(a: TodayFocusItem, b: TodayFocusItem): number {
  if (a.severityRank !== b.severityRank) {
    return b.severityRank - a.severityRank;
  }
  const timeA = parseTimestampMs(a.timestamp);
  const timeB = parseTimestampMs(b.timestamp);
  if (timeA !== timeB) {
    return timeB - timeA;
  }
  const machineCmp = (a.machineId || '').localeCompare(b.machineId || '');
  if (machineCmp !== 0) return machineCmp;
  return a.id.localeCompare(b.id);
}

function buildMachineReadings(
  machine?: Machine | null,
  system?: TodayFocusSystem | null,
): Array<{ label: string; reading: FormattedReading }> {
  const baseCap = resolveCapabilityStatus('base', machine, system);
  const tempCap = resolveCapabilityStatus('temperature', machine, system);
  const containerCap = resolveCapabilityStatus('containers', machine, system);

  return [
    {
      label: 'CPU',
      reading: formatMetricReading(system?.cpu, '%', baseCap),
    },
    {
      label: '内存',
      reading: formatMetricReading(system?.memory_pct, '%', baseCap),
    },
    {
      label: '磁盘',
      reading: formatMetricReading(system?.disk_pct, '%', baseCap),
    },
    {
      label: '温度',
      reading: formatMetricReading(normalizeTemperatureReading(machine, system), '°C', tempCap),
    },
    {
      label: '容器',
      reading: formatMetricReading(system?.container_count, '', containerCap),
    },
  ];
}

export interface BuildTodayFocusInput {
  machines: Machine[];
  systems: TodayFocusSystem[];
  observability?: ObservabilityPayload | null;
  services?: Service[];
  attentionItems?: AttentionItem[];
  /** 快照级来源健康：monitorUnavailable 或 MonitorSnapshot.ok === false */
  monitorUnavailable?: boolean;
}

/**
 * 构建并排序「今日关注」三分组（必做清单 1 & 2）：
 * - 故障组（置顶）：`data_quality` 为 `stale` / `source-failed` / `never-collected`（含 `partial` 与 `last_known_state === 'down'`）
 * - 到期组（居中）：复用现有续费（`isActiveRenewal` + ≤7 天）、流量配额（实测 ≥80% 或 `projection.over_quota`）、证书 SSL 与 Tailnet 到期逻辑
 * - 更新组（置底）：`agent_update_available === true` 的机器（B1-1 §5.4）及版本落后项
 */
export function buildTodayFocusGroups({
  machines,
  systems,
  observability,
  services = [],
  attentionItems = [],
  monitorUnavailable = false,
}: BuildTodayFocusInput): Record<TodayFocusGroupKey, TodayFocusItem[]> {
  const fault: TodayFocusItem[] = [];
  const expiring: TodayFocusItem[] = [];
  const update: TodayFocusItem[] = [];

  const machineById = new Map<string, Machine>();
  for (const m of machines) {
    machineById.set(m.id.toLowerCase(), m);
  }

  const systemByMachine = new Map<string, TodayFocusSystem>();
  for (const sys of systems) {
    const id = resolveMachineId(sys);
    if (id) systemByMachine.set(id, sys);
  }

  const seenMachineIds = new Set<string>();
  const pairs: Array<{ machineId: string; machine?: Machine; system?: TodayFocusSystem }> = [];

  for (const m of machines) {
    const id = m.id.toLowerCase();
    seenMachineIds.add(id);
    pairs.push({ machineId: id, machine: m, system: systemByMachine.get(id) });
  }
  for (const sys of systems) {
    const id = resolveMachineId(sys);
    if (!id || seenMachineIds.has(id)) continue;
    seenMachineIds.add(id);
    pairs.push({ machineId: id, machine: machineById.get(id), system: sys });
  }

  const seenExpiringKeys = new Set<string>();
  const seenFaultKeys = new Set<string>();
  const seenUpdateKeys = new Set<string>();

  for (const { machineId, machine, system } of pairs) {
    const label = machine?.label || system?.name || machineId.toUpperCase();
    const quality = resolveDataQuality(machine, system, { sourceFailed: monitorUnavailable });
    const lastKnown = resolveLastKnownState(machine, system);
    const sampledAt = system?.sampled_at ?? null;
    const readings = buildMachineReadings(machine, system);

    // 1. 故障组：data_quality ∈ {source-failed, never-collected, stale, partial} 或 last_known_state === 'down'
    const isQualityFault =
      quality === 'stale' ||
      quality === 'source-failed' ||
      quality === 'never-collected' ||
      quality === 'partial';
    const isDown = lastKnown === 'down';

    if (isQualityFault || isDown) {
      const qMeta = DATA_QUALITY_META[quality];
      const isError = quality === 'source-failed' || isDown;
      const severity: AttentionSeverity = isError ? 'error' : 'warn';
      const severityRank = (isDown ? 450 : 0) + qMeta.priority;

      const stateText =
        lastKnown === 'up' ? '最后已知在线' : lastKnown === 'down' ? '最后已知离线' : '运行状态未知';

      let detail = stateText;
      if (quality === 'source-failed') {
        detail = `${stateText} · 显示最后已知数据`;
      } else if (quality === 'stale') {
        detail = `${stateText} · 采样距今已超 12 分钟阈值`;
      } else if (quality === 'never-collected') {
        detail = '保留窗口内尚未收到任何采集样本';
      } else if (quality === 'partial') {
        const missing =
          system?.missing_fields && system.missing_fields.length > 0
            ? system.missing_fields.join('、')
            : readings
                .filter((r) => r.reading.state === 'missing')
                .map((r) => r.label)
                .join('、');
        detail = `${stateText} · 暂缺字段：${missing || '部分指标'}`;
      } else if (isDown) {
        detail = machine?.probe_msg || '设备离线或探测不可达';
      }

      const badge = isDown && quality === 'fresh' ? '设备离线' : qMeta.label;
      seenFaultKeys.add(`machine:${machineId}`);
      fault.push({
        id: `fault-machine-${machineId}`,
        group: 'fault',
        machineId,
        machineLabel: label,
        severity,
        severityRank,
        dataQuality: quality,
        lastKnownState: lastKnown,
        badge,
        title: `${label} · ${badge}`,
        detail,
        timestamp: sampledAt,
        href: `/devices/${machineId}?focus=fault`,
        readings,
      });
    }

    // 2. 到期组（复用现有续费与流量配额规则，不新建）
    const renewal = machine?.renewal;
    if (renewal && isActiveRenewal(renewal) && renewal.days_left != null && renewal.days_left <= 7) {
      const expired = renewal.days_left < 0;
      const price = priceText(renewal);
      const dayText = expired
        ? `已过期 ${Math.abs(renewal.days_left)} 天`
        : renewal.days_left === 0
          ? '今日到期'
          : `剩余 ${renewal.days_left} 天`;
      const detailParts = [
        dayText,
        renewal.expires_at ? `到期日 ${renewal.expires_at}` : null,
        price,
      ].filter(Boolean);

      // 越快到期（或已过期越久）排序权重越高
      const urgencyBonus = Math.max(0, 30 - renewal.days_left);
      seenExpiringKeys.add(`renewal:${machineId}`);
      expiring.push({
        id: `expiring-renewal-${machineId}`,
        group: 'expiring',
        machineId,
        machineLabel: label,
        severity: expired ? 'error' : 'warn',
        severityRank: (expired ? 300 : 200) + urgencyBonus,
        badge: expired ? '续费已期' : '续费临期',
        title: `${label} 续费${expired ? '已过期' : '即将到期'}`,
        detail: detailParts.join(' · '),
        timestamp: sampledAt,
        href: `/devices/${machineId}?focus=expiring`,
      });
    }

    const traffic = machine?.traffic;
    if (traffic && traffic.quota_gb != null && traffic.quota_gb > 0) {
      if (traffic.pct != null && traffic.pct >= 80) {
        const over = traffic.pct >= 100;
        seenExpiringKeys.add(`traffic:${machineId}`);
        expiring.push({
          id: `expiring-traffic-${machineId}`,
          group: 'expiring',
          machineId,
          machineLabel: label,
          severity: over ? 'error' : 'warn',
          severityRank: (over ? 280 : 180) + Math.min(50, Math.round(traffic.pct - 80)),
          badge: over ? '流量超额' : '流量预警',
          title: `${label} 月流量${over ? '已超配额' : '接近配额'}`,
          detail: `${traffic.month} 已用 ${traffic.used_gb.toFixed(1)} / ${traffic.quota_gb.toFixed(1)} GB（${traffic.pct.toFixed(1)}%）`,
          timestamp: traffic.updated_at ?? sampledAt,
          href: `/devices/${machineId}?focus=expiring`,
        });
      } else if (traffic.projection?.over_quota === true) {
        const projDetail = projectionText(traffic);
        if (projDetail) {
          seenExpiringKeys.add(`traffic-forecast:${machineId}`);
          expiring.push({
            id: `expiring-traffic-forecast-${machineId}`,
            group: 'expiring',
            machineId,
            machineLabel: label,
            // M1 契约：预测超配额恒为 warn，不升级为 error
            severity: 'warn',
            severityRank: 150,
            badge: '预计超额',
            title: `${label} 月流量预计超配额`,
            detail: projDetail,
            timestamp: traffic.updated_at ?? sampledAt,
            href: `/devices/${machineId}?focus=expiring`,
          });
        }
      }
    }

    // 3. 更新组：B1-1 §5.4 仅 agent_update_available === true 入组（null/false 不入）
    if (system?.agent_update_available === true) {
      const ver = system.agent_version ? `当前 v${system.agent_version}` : '当前版本已知';
      seenUpdateKeys.add(`agent-update:${machineId}`);
      update.push({
        id: `update-agent-${machineId}`,
        group: 'update',
        machineId,
        machineLabel: label,
        severity: 'info',
        severityRank: 100,
        badge: 'Agent 可更新',
        title: `${label} 采集 Agent 可更新`,
        detail: `${ver} · 更新提示不影响运行状态与数据质量`,
        timestamp: sampledAt,
        href: `/devices/${machineId}?filter=outdated&focus=update`,
      });
    }
  }

  // 复用传入的服务证书到期信息（T3 ssl_days / ssl_ok）
  for (const svc of services) {
    const sslDays = svc.health?.ssl_days;
    const sslOk = svc.health?.ssl_ok;
    if (sslOk === false || (sslDays != null && sslDays <= 14)) {
      const mId = String(svc.machine || '').toLowerCase();
      const expired = sslOk === false || (sslDays != null && sslDays < 0);
      const dayDesc =
        sslDays != null
          ? sslDays < 0
            ? `证书已过期 ${Math.abs(sslDays)} 天`
            : `证书剩余 ${sslDays} 天`
          : svc.health?.msg || 'SSL 证书校验异常';
      expiring.push({
        id: `expiring-cert-${mId}-${svc.name}`,
        group: 'expiring',
        machineId: mId || undefined,
        severity: expired ? 'error' : 'warn',
        severityRank: expired ? 290 : 190,
        badge: '证书到期',
        title: `${svc.name} 证书${expired ? '已失效' : '即将到期'}`,
        detail: dayDesc,
        href: mId ? `/devices/${mId}?focus=expiring` : '/devices?focus=expiring',
      });
    }
  }

  // 合并 observability 活动告警（若机器未因离线/质量入故障组，或补充具体规则告警）
  if (observability?.active_alerts) {
    for (const alert of observability.active_alerts) {
      const mId = (alert.machine || '').toLowerCase();
      const mLabel = machineById.get(mId)?.label || mId.toUpperCase() || '未知设备';
      const isErr = alert.severity === 'error';
      fault.push({
        id: `fault-alert-${mId}-${alert.name}`,
        group: 'fault',
        machineId: mId || undefined,
        machineLabel: mLabel,
        severity: isErr ? 'error' : 'warn',
        severityRank: isErr ? 260 : 160,
        badge: '活动告警',
        title: `${mLabel} · ${alert.name}`,
        detail:
          alert.threshold != null
            ? `阈值 ${alert.threshold}${alert.duration_min ? ` · 持续 ${alert.duration_min} 分钟` : ''}`
            : '告警触发中',
        timestamp: alert.triggered_at,
        href: mId ? `/devices/${mId}?focus=fault` : '/devices?focus=fault',
      });
    }
  }

  // 复用后端 `/api/attention` 传入的既有条目（按组归类并去重）
  for (const item of attentionItems) {
    const kind = item.kind;
    if (kind === 'version') {
      const key = `version:${item.title}`;
      if (seenUpdateKeys.has(key)) continue;
      seenUpdateKeys.add(key);
      update.push({
        id: `update-attn-${item.dismiss_key || item.title}`,
        group: 'update',
        severity: 'info',
        severityRank: 90,
        badge: '版本更新',
        title: item.title,
        detail: item.detail,
        href: item.link || '/devices?filter=outdated&focus=update',
      });
    } else if (
      kind === 'renewal' ||
      kind === 'tailnet' ||
      kind === 'traffic' ||
      kind === 'traffic-forecast' ||
      kind === 'cert' ||
      kind === 'ssl'
    ) {
      const matchedMachine = machines.find(
        (m) =>
          item.link === `/devices/${m.id}` ||
          item.title.toLowerCase().startsWith(m.id.toLowerCase()) ||
          item.title.startsWith(m.label),
      );
      if (matchedMachine) {
        const mId = matchedMachine.id.toLowerCase();
        if (kind === 'renewal' && seenExpiringKeys.has(`renewal:${mId}`)) continue;
        if (kind === 'traffic' && seenExpiringKeys.has(`traffic:${mId}`)) continue;
        if (kind === 'traffic-forecast' && seenExpiringKeys.has(`traffic-forecast:${mId}`)) continue;
      }
      const isErr = item.severity === 'error';
      expiring.push({
        id: `expiring-attn-${item.dismiss_key || `${item.kind}-${item.title}`}`,
        group: 'expiring',
        machineId: matchedMachine?.id,
        severity: isErr ? 'error' : 'warn',
        severityRank: isErr ? 250 : 150,
        badge:
          kind === 'renewal'
            ? '续费'
            : kind === 'tailnet'
              ? 'Tailnet 密钥'
              : kind === 'traffic' || kind === 'traffic-forecast'
                ? '流量配额'
                : '证书到期',
        title: item.title,
        detail: item.detail,
        href: item.link || '/devices?focus=expiring',
      });
    } else {
      const matchedMachine = machines.find(
        (m) =>
          item.link === `/devices/${m.id}` ||
          item.title.toLowerCase().startsWith(m.id.toLowerCase()),
      );
      if (matchedMachine && kind === 'machine' && seenFaultKeys.has(`machine:${matchedMachine.id.toLowerCase()}`)) {
        continue;
      }
      const isErr = item.severity === 'error';
      fault.push({
        id: `fault-attn-${item.dismiss_key || `${item.kind}-${item.title}`}`,
        group: 'fault',
        machineId: matchedMachine?.id,
        severity: isErr ? 'error' : 'warn',
        severityRank: isErr ? 350 : 180,
        badge: isErr ? '故障' : '留意',
        title: item.title,
        detail: item.detail,
        href: item.link || '/devices?focus=fault',
      });
    }
  }

  fault.sort(compareTodayFocusItems);
  expiring.sort(compareTodayFocusItems);
  update.sort(compareTodayFocusItems);

  return { fault, expiring, update };
}

// ---------------------------------------------------------------------------
// TodayFocus 组件（必做清单 4 & 5：分组卡片 + 计数徽章 + 点击直达筛选 + B1 对齐）
// ---------------------------------------------------------------------------

export interface TodayFocusProps {
  machines: Machine[];
  systems: TodayFocusSystem[];
  observability?: ObservabilityPayload | null;
  services?: Service[];
  /** 可选注入 `/api/attention` 条目，与机器快照推导结果自动合并去重 */
  attentionItems?: AttentionItem[];
  /** 快照级来源失败标记（对应 MonitorSnapshot.ok === false 或拉取异常） */
  monitorUnavailable?: boolean;
  /** 当前激活的分组筛选（与 B2-3 总控台筛选联动） */
  activeGroup?: TodayFocusGroupKey | null;
  /** 点击分组筛选时的回调；不传则使用默认路由跳转 */
  onSelectGroup?: (group: TodayFocusGroupKey | null) => void;
  /** 点击具体关注条目时的回调；不传则导航到 item.href */
  onSelectItem?: (item: TodayFocusItem) => void;
  className?: string;
}

const GROUP_ICONS: Record<TodayFocusGroupKey, React.ComponentType<{ className?: string }>> = {
  fault: AlertTriangle,
  expiring: CalendarClock,
  update: RefreshCw,
};

function itemBadgeTone(item: TodayFocusItem): string {
  if (item.group === 'update') {
    // B1-1 §5.4：更新组恒用中性色，绝不与故障/警告混淆
    return 'bg-muted text-muted-foreground border-border/60';
  }
  if (item.dataQuality) {
    return DATA_QUALITY_META[item.dataQuality].badgeClass;
  }
  if (item.severity === 'error') {
    return 'bg-destructive/15 text-destructive border-destructive/30';
  }
  return 'bg-amber-500/15 text-amber-700 dark:text-amber-400 border-amber-500/30';
}

export function TodayFocus({
  machines,
  systems,
  observability,
  services,
  attentionItems,
  monitorUnavailable = false,
  activeGroup = null,
  onSelectGroup,
  onSelectItem,
  className,
}: TodayFocusProps) {
  const navigate = useNavigate();

  const coverage = useMemo(
    () => computeMetricCoverage(machines, systems, { sourceFailed: monitorUnavailable }),
    [machines, systems, monitorUnavailable],
  );

  const groups = useMemo(
    () =>
      buildTodayFocusGroups({
        machines,
        systems,
        observability,
        services,
        attentionItems,
        monitorUnavailable,
      }),
    [machines, systems, observability, services, attentionItems, monitorUnavailable],
  );

  const totalCount =
    groups.fault.length + groups.expiring.length + groups.update.length;

  const visibleGroupKeys = useMemo(
    () =>
      TODAY_FOCUS_GROUP_ORDER.filter((key) =>
        activeGroup ? key === activeGroup : groups[key].length > 0,
      ),
    [activeGroup, groups],
  );

  const handleGroupClick = (key: TodayFocusGroupKey, href: string) => {
    if (onSelectGroup) {
      onSelectGroup(activeGroup === key ? null : key);
      return;
    }
    navigate(href);
  };

  const handleItemClick = (item: TodayFocusItem) => {
    if (onSelectItem) {
      onSelectItem(item);
      return;
    }
    navigate(item.href);
  };

  return (
    <section
      aria-label="今日关注"
      data-today-focus
      className={cn('space-y-3', className)}
    >
      <Card>
        <CardHeader className="flex flex-col gap-2 space-y-0 pb-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle className="text-base font-semibold">今日关注</CardTitle>
            <span
              className="rounded-full bg-muted px-2 py-0.5 font-mono text-xs tabular-nums text-muted-foreground"
              data-testid="today-focus-total"
            >
              {totalCount === 0 ? '无待办' : `${totalCount} 项`}
            </span>
            {monitorUnavailable && (
              <span className="rounded border border-destructive/30 bg-destructive/15 px-1.5 py-0.5 text-xs text-destructive">
                来源失败 · 显示最后已知数据
              </span>
            )}
          </div>

          {/* 分组计数徽章条：故障置顶 → 到期 → 更新置底，点击直达对应筛选 */}
          <div
            role="group"
            aria-label="今日关注分组筛选"
            className="flex flex-wrap items-center gap-1.5"
          >
            {TODAY_FOCUS_GROUP_ORDER.map((groupKey) => {
              const meta = TODAY_FOCUS_GROUPS[groupKey];
              const count = groups[groupKey].length;
              const isSelected = activeGroup === groupKey;
              return (
                <button
                  key={groupKey}
                  type="button"
                  aria-pressed={isSelected}
                  data-focus-group-badge={groupKey}
                  onClick={() => handleGroupClick(groupKey, meta.filterHref)}
                  className={cn(
                    'inline-flex min-h-8 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors',
                    isSelected
                      ? 'border-primary bg-primary/10 font-medium text-primary'
                      : 'border-border/60 bg-card hover:border-primary/40',
                  )}
                >
                  <span>{meta.title}</span>
                  <span
                    className={cn(
                      'rounded-full px-1.5 py-0.5 font-mono text-[11px] font-semibold tabular-nums',
                      count > 0 ? meta.badgeClass : 'bg-muted text-muted-foreground',
                    )}
                  >
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        </CardHeader>

        <CardContent className="space-y-3 pt-0">
          {/* 统计分母与指标覆盖条（B1-1 §8.3：明确有读数的机器数分母，不用全量台数冒充） */}
          <div
            data-testid="today-focus-coverage"
            className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
          >
            <span className="font-medium text-foreground">
              采样覆盖：{coverage.freshCount}/{coverage.registeredTotal} 台实时
            </span>
            {(
              [
                coverage.metrics.cpu,
                coverage.metrics.memory_pct,
                coverage.metrics.disk_pct,
                coverage.metrics.temperature,
                coverage.metrics.container_count,
              ] as const
            ).map((stat) => (
              <span
                key={stat.metric}
                data-coverage-metric={stat.metric}
                className="inline-flex items-center gap-1 font-mono tabular-nums"
                title={
                  `有效读数分母：${stat.validCount}/${stat.supportedCount} 台支持机器` +
                  (stat.unsupportedCount > 0 ? `（${stat.unsupportedCount} 台不支持）` : '') +
                  (stat.missingCount > 0 ? `，${stat.missingCount} 台暂缺` : '')
                }
              >
                <span className="font-sans text-muted-foreground">{stat.label}</span>
                <span className="text-foreground">
                  {stat.average != null
                    ? `均值 ${Math.round(stat.average)}${stat.unit}`
                    : '—'}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  ({stat.validCount}/{stat.supportedCount}台可读
                  {stat.unsupportedCount > 0 ? ` · ${stat.unsupportedCount}不支持` : ''})
                </span>
              </span>
            ))}
          </div>

          {totalCount === 0 ? (
            <div
              role="status"
              className="flex items-center gap-2 rounded-lg bg-emerald-500/5 px-3 py-2.5 text-sm text-muted-foreground"
            >
              <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
              <span>今日无待关注，采样质量、到期状态与版本均正常</span>
            </div>
          ) : (
            <div className="grid gap-3">
              {visibleGroupKeys.map((groupKey) => {
                const meta = TODAY_FOCUS_GROUPS[groupKey];
                const items = groups[groupKey];
                const Icon = GROUP_ICONS[groupKey];

                return (
                  <div
                    key={groupKey}
                    data-focus-group={groupKey}
                    className={cn(
                      'rounded-lg border bg-card/60',
                      groupKey === 'fault' && items.length > 0 && 'border-destructive/30',
                      groupKey === 'expiring' && items.length > 0 && 'border-amber-500/30',
                      groupKey === 'update' && 'border-border/60 bg-muted/20',
                    )}
                  >
                    <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
                      <div className="flex min-w-0 items-center gap-2">
                        <Icon
                          className={cn(
                            'h-4 w-4 shrink-0',
                            groupKey === 'fault'
                              ? 'text-destructive'
                              : groupKey === 'expiring'
                                ? 'text-amber-600 dark:text-amber-400'
                                : 'text-muted-foreground',
                          )}
                        />
                        <h3 className="truncate text-sm font-medium">{meta.title}</h3>
                        <span
                          className={cn(
                            'rounded-full px-2 py-0.5 font-mono text-xs font-semibold tabular-nums',
                            meta.badgeClass,
                          )}
                        >
                          {items.length}
                        </span>
                      </div>
                      <Link
                        to={meta.filterHref}
                        onClick={(e) => {
                          if (onSelectGroup) {
                            e.preventDefault();
                            onSelectGroup(groupKey);
                          }
                        }}
                        className="inline-flex min-h-8 shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                      >
                        <span>筛选本组</span>
                        <ArrowUpRight className="h-3.5 w-3.5" />
                      </Link>
                    </div>

                    {items.length === 0 ? (
                      <p className="px-3 py-2.5 text-xs text-muted-foreground">
                        当前分组无待关注项
                      </p>
                    ) : (
                      <div className="divide-y">
                        {items.map((item) => (
                          <button
                            key={item.id}
                            type="button"
                            data-focus-item={item.id}
                            onClick={() => handleItemClick(item)}
                            className="flex w-full flex-col gap-1.5 px-3 py-2.5 text-left transition-colors hover:bg-muted/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring sm:flex-row sm:items-center sm:justify-between"
                          >
                            <div className="min-w-0 flex-1 space-y-1">
                              <div className="flex flex-wrap items-center gap-2">
                                <span
                                  className={cn(
                                    'shrink-0 rounded border px-1.5 py-0.5 text-[11px] font-medium',
                                    itemBadgeTone(item),
                                  )}
                                >
                                  {item.badge}
                                </span>
                                <span className="truncate text-sm font-medium">
                                  {item.title}
                                </span>
                                {item.timestamp && (
                                  <span className="text-xs text-muted-foreground">
                                    截至 <DisplayTime value={item.timestamp} />
                                  </span>
                                )}
                              </div>
                              <p className="truncate text-xs text-muted-foreground">
                                {item.detail}
                              </p>
                            </div>

                            {item.readings && item.readings.length > 0 && (
                              <div className="flex shrink-0 flex-wrap items-center gap-2 text-xs">
                                {item.readings.map(({ label, reading }) => (
                                  <span
                                    key={label}
                                    title={reading.hint}
                                    className={cn(
                                      'inline-flex items-center gap-1 rounded bg-muted/50 px-1.5 py-0.5 font-mono tabular-nums',
                                      reading.state !== 'valid' && 'text-muted-foreground',
                                    )}
                                  >
                                    <span className="font-sans text-muted-foreground">
                                      {label}
                                    </span>
                                    <span>{reading.text}</span>
                                  </span>
                                ))}
                              </div>
                            )}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
