import { Link } from 'react-router-dom';
import { ArrowUpRight, Cpu, HardDrive, MemoryStick } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DisplayTime } from '@/components/ui/display-time';
import { meterTone, statusTone } from '@/components/monitor/machine-card';
import { cn } from '@/lib/utils';
import type { Machine, MonitorSystem } from '@/lib/types';

// ---------------------------------------------------------------------------
// B1 冻结契约类型（B1-1 §4.1/§4.4/§8.1、B1-2 §3/§6、B1-3 §3/§6.2）
// ---------------------------------------------------------------------------

/** B1-1 §5.1：最后已知运行状态（允许过期，不因 stale/source-failed 自动改写为 unknown）。 */
export type LastKnownState = 'up' | 'down' | 'unknown';

/** B1-2 §3.1 / §6：五态数据质量展示枚举（后端 authority 判定，前端不自行按本地时钟重算）。 */
export type DataQualityState =
  | 'fresh'
  | 'stale'
  | 'source-failed'
  | 'partial'
  | 'never-collected';

/** B1-1 §4.4：单指标能力声明状态。 */
export type CapabilityStatus = 'supported' | 'partial' | 'unsupported';

export interface MetricCapability {
  status: CapabilityStatus;
  note?: string;
}

/** B2-2 设备概况卡展示的三个关键读数键。 */
export type OverviewMetricKey = 'cpu' | 'memory_pct' | 'disk_pct';

/** B1-3 §3.1：历史缺口原因枚举。 */
export type HistoryGapReason =
  | 'no_samples'
  | 'retention_exceeded'
  | 'archived'
  | 'unsupported';

export interface HistoryGap {
  start: string;
  end: string;
  reason: HistoryGapReason;
}

/** 兼容 B1-3 §6.2 `{ts, value}` 与 B1-1 §4.3 `{t, v}` 两种历史点结构。 */
export type HistoryPoint =
  | { ts: string; value: number }
  | { t: string; v: number };

/** B1-3 §6.2：24h 紧凑趋势序列输入。 */
export interface CompactTrendSeries {
  metric?: string;
  metric_key?: string;
  unit?: string;
  /** 系列实际采样间隔（秒），默认 300s（B1-3 §2.2 raw 档）。 */
  interval_sec?: number;
  points: HistoryPoint[];
  gaps?: HistoryGap[];
  window?: {
    start: string;
    end: string;
    effective_end?: string;
  };
}

/**
 * B1-1 §4.1 / §8.1 MachineSnapshot 前端消费接口。
 * 向下兼容现有 `MonitorSystem`，允许在过渡期直接传入 `MonitorSystem`。
 */
export interface MachineOverviewSnapshot extends Partial<MonitorSystem> {
  /** B1-1 §2.1：台账稳定主键（小写：nas/txy/aly/kr/sg/us）。 */
  machine_id?: string;
  /** B1-1 §5.1：最后已知运行状态。 */
  last_known_state?: LastKnownState;
  /** B1-2 §6：五态数据质量展示枚举。 */
  data_quality?: DataQualityState;
  /** B1-1 §2.3：机器级主源标识。 */
  source?: 'beszel' | 'go-agent' | 'ssh-fallback';
  /** B1-1 §3：机器级实际采样时间（ISO 8601 UTC），从未采到为 null。 */
  sampled_at?: string | null;
  /** B1-1 §4.6：名义采样间隔（秒），仅作提示值。 */
  sample_interval_sec?: number | null;
  /** B1-1 §5.4：Agent 版本号。 */
  agent_version?: string | null;
  /** B1-1 §5.4：Agent 更新提示（非故障，视觉与异常严格分离）。 */
  agent_update_available?: boolean | null;
  /** B1-1 §4.4 / B1-2 §4.1：按指标或能力组声明的能力状态。 */
  capabilities?: Partial<Record<string, MetricCapability>>;
  /** B1-2 §4.1：本应有值但本次缺失的字段清单（不含能力缺失）。 */
  missing_fields?: string[];
  cpu?: number | null;
  memory_pct?: number | null;
  disk_pct?: number | null;
}

// ---------------------------------------------------------------------------
// 状态、缺失与趋势纯函数（导出供 B2-1/B2-3 与单测复用）
// ---------------------------------------------------------------------------

/**
 * 解析机器稳定 ID（B1-1 §2.1）：台账 `machine.id` 优先，其次 `snapshot.machine_id` / `snapshot.machine`。
 * 不拿 provider 内部 `snapshot.id` 冒充台账身份。
 */
export function resolveMachineId(
  machine?: Pick<Machine, 'id'> | null,
  snapshot?: MachineOverviewSnapshot | null
): string | null {
  const id = machine?.id || snapshot?.machine_id || snapshot?.machine;
  return id ? id.trim() || null : null;
}

/**
 * 解析最后已知运行状态（B1-1 §5.1）。
 * 规则：来源失败或数据过期时保留最后已知的 `up`/`down`，不自动改写为 `unknown`。
 */
export function resolveLastKnownState(
  machine?: Pick<Machine, 'role' | 'probe_status'> | null,
  snapshot?: MachineOverviewSnapshot | null
): LastKnownState {
  if (
    snapshot?.last_known_state === 'up' ||
    snapshot?.last_known_state === 'down' ||
    snapshot?.last_known_state === 'unknown'
  ) {
    return snapshot.last_known_state;
  }
  const raw = machine?.role === 'node' ? machine.probe_status : snapshot?.status;
  if (raw === 'up' || raw === 'down') return raw;
  return 'unknown';
}

/**
 * 解析五态数据质量枚举（B1-2 §3.1）：
 * 优先级：`source-failed` > `never-collected` > `stale` > `partial` > `fresh`。
 *
 * 严格遵循 B1-2 §1：前端只消费后端已判定的状态枚举与 `sourceFailed` 信号，
 * 绝不用浏览器本地时钟重算 12 分钟过期阈值。
 */
export function resolveDataQuality(
  snapshot?: MachineOverviewSnapshot | null,
  options?: { sourceFailed?: boolean }
): DataQualityState {
  if (options?.sourceFailed) return 'source-failed';
  if (snapshot?.data_quality) return snapshot.data_quality;
  if (!snapshot) return 'never-collected';

  const hasAnyReading =
    snapshot.cpu != null ||
    snapshot.memory_pct != null ||
    snapshot.disk_pct != null;
  if (!snapshot.sampled_at && !hasAnyReading && !snapshot.status && !snapshot.last_known_state) {
    return 'never-collected';
  }
  if (snapshot.missing_fields && snapshot.missing_fields.length > 0) {
    return 'partial';
  }
  return 'fresh';
}

/** B1-2 §4.2 / §6：五态展示文案与视觉样式映射。 */
export const DATA_QUALITY_META: Record<
  DataQualityState,
  { label: string | null; badgeClass: string; stateLabel: string }
> = {
  fresh: {
    label: null,
    badgeClass: '',
    stateLabel: '实时',
  },
  stale: {
    label: '数据过期',
    badgeClass: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
    stateLabel: '数据过期',
  },
  'source-failed': {
    label: '来源失败',
    badgeClass: 'bg-destructive/15 text-destructive',
    stateLabel: '来源失败',
  },
  partial: {
    label: '部分指标缺失',
    badgeClass: 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
    stateLabel: '部分指标缺失',
  },
  'never-collected': {
    label: '暂无数据',
    badgeClass: 'bg-muted text-muted-foreground',
    stateLabel: '暂无数据',
  },
};

const LAST_KNOWN_LABELS: Record<LastKnownState, string> = {
  up: '在线',
  down: '离线',
  unknown: '状态未知',
};

const METRIC_CAPABILITY_ALIASES: Record<OverviewMetricKey, string[]> = {
  cpu: ['cpu', 'cpu.used_pct', 'base'],
  memory_pct: ['memory_pct', 'memory', 'memory.used_pct', 'base'],
  disk_pct: ['disk_pct', 'disk', 'disk.used_pct', 'base'],
};

const METRIC_MISSING_ALIASES: Record<OverviewMetricKey, string[]> = {
  cpu: ['cpu', 'cpu_pct', 'cpu.used_pct'],
  memory_pct: ['memory_pct', 'memory', 'memory.used_pct'],
  disk_pct: ['disk_pct', 'disk', 'disk.used_pct'],
};

export interface MetricDisplayResult {
  kind: 'value' | 'missing' | 'unsupported';
  /** 主展示文本：正常值如 `43.2%`，数据缺失为 `—`，能力缺失为 `不支持`。 */
  text: string;
  /** 仅当 `kind === 'value'` 时为 0–100 数值，其余恒为 null（严禁 0 兜底）。 */
  pct: number | null;
  /** 辅助说明（进 title 或副标记，如 `暂缺` / `不支持`）。 */
  hint?: string;
}

/**
 * 单指标展示判定（B1-1 §6 / B1-2 §3.3 & §4.1 & §4.2）：
 * - 能力缺失（`unsupported`）→ 标记「不支持」，不画进度条，不算异常
 * - 数据缺失（`null` / `undefined`）→ 显示横线 `—`（绝不用 `?? 0` 兜底成 0%）
 * - 真实 0 值 → 正常渲染 `0.0%`
 */
export function resolveMetricDisplay(
  key: OverviewMetricKey,
  snapshot?: MachineOverviewSnapshot | null,
  quality?: DataQualityState
): MetricDisplayResult {
  const caps = snapshot?.capabilities;
  if (caps) {
    for (const alias of METRIC_CAPABILITY_ALIASES[key]) {
      if (caps[alias]?.status === 'unsupported') {
        return {
          kind: 'unsupported',
          text: '不支持',
          pct: null,
          hint: '该设备不支持此项指标',
        };
      }
    }
  }

  if (quality === 'never-collected') {
    return {
      kind: 'missing',
      text: '—',
      pct: null,
      hint: '暂无数据',
    };
  }

  const raw = snapshot?.[key];
  if (raw == null || !Number.isFinite(raw)) {
    const missingSet = new Set(snapshot?.missing_fields || []);
    const flaggedMissing = METRIC_MISSING_ALIASES[key].some((alias) => missingSet.has(alias));
    return {
      kind: 'missing',
      text: '—',
      pct: null,
      hint: flaggedMissing || quality === 'partial' ? '暂缺' : '暂无读数',
    };
  }

  const clamped = Math.min(100, Math.max(0, raw));
  return {
    kind: 'value',
    text: `${raw.toFixed(1)}%`,
    pct: clamped,
  };
}

// ---------------------------------------------------------------------------
// 24h 紧凑趋势迷你图计算（B1-3 §3 / §6.4：缺口断线，缺失历史绝不画假趋势）
// ---------------------------------------------------------------------------

export interface SparklineCoord {
  x: number;
  y: number;
  ts: number;
  value: number;
}

export interface SparklineGapRange {
  x1: number;
  x2: number;
  reason: HistoryGapReason;
}

export interface SparklineModel {
  hasTrend: boolean;
  unsupported: boolean;
  segments: SparklineCoord[][];
  gaps: SparklineGapRange[];
  latestValue: number | null;
}

function normalizePoint(p: HistoryPoint): { ts: number; value: number } | null {
  const rawTs = 'ts' in p ? p.ts : p.t;
  const rawVal = 'value' in p ? p.value : p.v;
  if (rawVal == null || !Number.isFinite(rawVal)) return null;
  const ms = Date.parse(rawTs);
  if (!Number.isFinite(ms)) return null;
  return { ts: ms, value: rawVal };
}

/**
 * 将 B1-3 历史序列转换为分段迷你折线坐标（viewBox 宽 120 × 高 24）。
 * - 有效点 < 2 或 `never-collected` 或 `unsupported` 时 `hasTrend = false`（不画假趋势）。
 * - 相邻点跨越 `gaps` 区间或间隔超过 `2.5 × interval_sec`（对应 12 分钟断更阈值）时断线。
 */
export function buildSparklineModel(
  series?: CompactTrendSeries | null,
  quality?: DataQualityState,
  width = 120,
  height = 24
): SparklineModel {
  const rawGaps = series?.gaps || [];
  if (rawGaps.some((g) => g.reason === 'unsupported')) {
    return { hasTrend: false, unsupported: true, segments: [], gaps: [], latestValue: null };
  }
  if (!series || quality === 'never-collected') {
    return { hasTrend: false, unsupported: false, segments: [], gaps: [], latestValue: null };
  }

  const normalized = (series.points || [])
    .map(normalizePoint)
    .filter((item): item is { ts: number; value: number } => item !== null)
    .sort((a, b) => a.ts - b.ts);

  if (normalized.length < 2) {
    return {
      hasTrend: false,
      unsupported: false,
      segments: [],
      gaps: [],
      latestValue: normalized[0]?.value ?? null,
    };
  }

  const parsedGaps = rawGaps
    .map((g) => ({
      start: Date.parse(g.start),
      end: Date.parse(g.end),
      reason: g.reason,
    }))
    .filter((g) => Number.isFinite(g.start) && Number.isFinite(g.end) && g.end > g.start);

  const winStart = series.window?.start ? Date.parse(series.window.start) : Number.NaN;
  const winEndRaw = series.window?.effective_end || series.window?.end;
  const winEnd = winEndRaw ? Date.parse(winEndRaw) : Number.NaN;

  const minTs = Number.isFinite(winStart)
    ? Math.min(winStart, normalized[0].ts)
    : Math.min(normalized[0].ts, ...parsedGaps.map((g) => g.start));
  const maxTs = Number.isFinite(winEnd)
    ? Math.max(winEnd, normalized[normalized.length - 1].ts)
    : Math.max(normalized[normalized.length - 1].ts, ...parsedGaps.map((g) => g.end));

  const spanTs = Math.max(1, maxTs - minTs);
  const isPercent = (series.unit || '%') === '%' || series.unit === 'percent';
  const values = normalized.map((p) => p.value);
  const minVal = isPercent ? 0 : Math.min(...values);
  const maxVal = isPercent ? 100 : Math.max(...values, minVal + 1);
  const valSpan = Math.max(1e-6, maxVal - minVal);

  const padY = 2;
  const usableH = height - padY * 2;
  const toX = (ts: number) => Number((((ts - minTs) / spanTs) * width).toFixed(2));
  const toY = (v: number) => {
    const ratio = Math.min(1, Math.max(0, (v - minVal) / valSpan));
    return Number((padY + usableH * (1 - ratio)).toFixed(2));
  };

  // B1-3 §3.2 / §6.4：超过采样间隔 2.5 倍（默认 300s * 2.5 = 750s ≈ 12.5m）或命中显式 gap 即断线
  const intervalMs = Math.max(60, series.interval_sec ?? 300) * 1000;
  const maxStepMs = intervalMs * 2.5;

  const segments: SparklineCoord[][] = [];
  let current: SparklineCoord[] = [];

  for (let i = 0; i < normalized.length; i += 1) {
    const pt = normalized[i];
    const coord: SparklineCoord = {
      x: toX(pt.ts),
      y: toY(pt.value),
      ts: pt.ts,
      value: pt.value,
    };
    if (i === 0) {
      current.push(coord);
      continue;
    }
    const prev = normalized[i - 1];
    const hitsExplicitGap = parsedGaps.some((g) => g.start < pt.ts && g.end > prev.ts);
    const exceedsStep = pt.ts - prev.ts > maxStepMs;
    if (hitsExplicitGap || exceedsStep) {
      if (current.length > 0) segments.push(current);
      current = [coord];
    } else {
      current.push(coord);
    }
  }
  if (current.length > 0) segments.push(current);

  const gaps: SparklineGapRange[] = parsedGaps.map((g) => ({
    x1: Math.max(0, toX(g.start)),
    x2: Math.min(width, toX(g.end)),
    reason: g.reason,
  }));

  return {
    hasTrend: segments.some((seg) => seg.length >= 2),
    unsupported: false,
    segments,
    gaps,
    latestValue: normalized[normalized.length - 1].value,
  };
}

// ---------------------------------------------------------------------------
// 子组件：关键读数格与 24h 迷你图
// ---------------------------------------------------------------------------

interface OverviewMetricItemProps {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  display: MetricDisplayResult;
}

function OverviewMetricItem({ icon: Icon, label, display }: OverviewMetricItemProps) {
  const isMissingOrUnsupported = display.kind !== 'value';
  return (
    <div
      className="flex min-w-0 flex-col gap-1 rounded-md bg-muted/40 px-2 py-1.5"
      title={display.hint}
      data-metric-state={display.kind}
    >
      <div className="flex items-baseline justify-between gap-1 text-xs">
        <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground">
          <Icon className="h-3 w-3 shrink-0" />
          <span className="truncate">{label}</span>
        </span>
        <span
          className={cn(
            'shrink-0 font-mono tabular-nums',
            isMissingOrUnsupported
              ? 'text-[11px] font-normal text-muted-foreground'
              : 'text-xs font-medium text-foreground'
          )}
        >
          {display.text}
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-muted-foreground/20">
        {display.pct != null && (
          <div
            className={cn('h-full rounded-full transition-all', meterTone(display.pct))}
            style={{ width: `${display.pct}%` }}
          />
        )}
      </div>
    </div>
  );
}

function CompactTrendSparkline({
  trend,
  quality,
}: {
  trend: CompactTrendSeries | null;
  quality: DataQualityState;
}) {
  const model = buildSparklineModel(trend, quality);
  const metricLabel = trend?.metric_key || trend?.metric || 'CPU';

  if (model.unsupported) {
    return (
      <div
        className="flex items-center justify-between rounded-md border border-dashed border-border/70 px-2 py-1 text-[11px] text-muted-foreground"
        data-trend-state="unsupported"
      >
        <span>24h 趋势</span>
        <span>不支持</span>
      </div>
    );
  }

  if (!model.hasTrend) {
    return (
      <div
        className="flex items-center justify-between rounded-md border border-dashed border-border/70 px-2 py-1 text-[11px] text-muted-foreground"
        data-trend-state="empty"
      >
        <span>24h 趋势</span>
        <span>暂无历史样本</span>
      </div>
    );
  }

  return (
    <div
      className="flex items-center gap-2 rounded-md bg-muted/30 px-2 py-1"
      data-trend-state="ready"
      title={`24h ${metricLabel} 趋势${model.gaps.length > 0 ? `（含 ${model.gaps.length} 处采样缺口）` : ''}`}
    >
      <span className="shrink-0 text-[11px] text-muted-foreground">24h</span>
      <svg
        viewBox="0 0 120 24"
        className="h-5 flex-1 overflow-visible"
        role="img"
        aria-label={`24 小时 ${metricLabel} 趋势迷你图`}
        preserveAspectRatio="none"
      >
        {model.gaps.map((g, idx) => (
          <rect
            key={`gap-${idx}`}
            x={g.x1}
            y={2}
            width={Math.max(2, g.x2 - g.x1)}
            height={20}
            className="fill-muted-foreground/15"
          />
        ))}
        {model.segments.map((seg, idx) =>
          seg.length >= 2 ? (
            <polyline
              key={`seg-${idx}`}
              fill="none"
              stroke="currentColor"
              strokeWidth={1.75}
              strokeLinecap="round"
              strokeLinejoin="round"
              className="text-primary/80"
              points={seg.map((pt) => `${pt.x},${pt.y}`).join(' ')}
            />
          ) : null
        )}
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// MachineOverviewCard 主组件（B2-2 交付物）
// ---------------------------------------------------------------------------

export interface MachineOverviewCardProps {
  /** 台账机器对象；若为未登记的孤儿系统可不传，由 snapshot 兜底显示名。 */
  machine?: Machine | null;
  /** B1 MachineSnapshot DTO（兼容现有 MonitorSystem）。 */
  snapshot?: MachineOverviewSnapshot | null;
  /** B1-3 24h 紧凑趋势序列；传入时渲染迷你趋势图，缺失历史时显式渲染缺口而非假趋势。 */
  trend?: CompactTrendSeries | null;
  /** 快照级来源失败标记（对应 MonitorSnapshot.ok === false 或 monitorUnavailable）。 */
  sourceFailed?: boolean;
  /** 自定义跳转目标路径；默认当存在台账 machineId 时直达 `/devices/${machineId}`，传 null 禁用链接。 */
  to?: string | null;
  /** 可选点击回调（供总控台筛选联动或自定义导航使用）。 */
  onClick?: () => void;
  className?: string;
}

/**
 * B2-2 总控台「设备概况卡」组件。
 *
 * 与 `machine-card.tsx` 的分工：
 * - 本组件用于 B2 总控台，紧凑呈现「状态 + 关键读数（CPU/内存/硬盘）+ 五态新鲜度 + 24h 迷你趋势 + 独立更新提示」，点击直达 `/devices/:id`。
 * - 原 `machine-card.tsx` 保留给其他页面使用，B3 阶段再统一收敛。
 */
export function MachineOverviewCard({
  machine,
  snapshot,
  trend,
  sourceFailed = false,
  to,
  onClick,
  className,
}: MachineOverviewCardProps) {
  const machineId = resolveMachineId(machine, snapshot);
  const label = machine?.label || snapshot?.name || machineId || '未知设备';
  const lastKnownState = resolveLastKnownState(machine, snapshot);
  const dataQuality = resolveDataQuality(snapshot, { sourceFailed });
  const qualityMeta = DATA_QUALITY_META[dataQuality];
  const sampledAt = snapshot?.sampled_at ?? null;

  // B1-1 §5.4：Agent 更新提示与故障/异常视觉严格分离
  const updateAvailable = snapshot?.agent_update_available === true;
  const agentVersion = snapshot?.agent_version ?? null;

  const cpuDisplay = resolveMetricDisplay('cpu', snapshot, dataQuality);
  const memDisplay = resolveMetricDisplay('memory_pct', snapshot, dataQuality);
  const diskDisplay = resolveMetricDisplay('disk_pct', snapshot, dataQuality);

  const targetHref = to !== undefined ? to : machineId ? `/devices/${machineId}` : null;
  const isInteractive = Boolean(targetHref || onClick);

  const cardInner = (
    <Card
      className={cn(
        'transition-colors',
        isInteractive && 'cursor-pointer hover:bg-card/90',
        className
      )}
      data-machine-overview-card={machineId || undefined}
      data-data-quality={dataQuality}
      data-last-known-state={lastKnownState}
      onClick={!targetHref ? onClick : undefined}
      {...(!targetHref && onClick
        ? {
            role: 'button',
            tabIndex: 0,
            onKeyDown: (e: React.KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick();
              }
            },
          }
        : {})}
    >
      {/* ---- Header：状态点 + 机器名 + 五态质量徽章 + 独立更新提示 ---- */}
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0 p-3 pb-2">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={cn('h-2.5 w-2.5 shrink-0 rounded-full', statusTone(lastKnownState))}
            title={LAST_KNOWN_LABELS[lastKnownState]}
            aria-label={LAST_KNOWN_LABELS[lastKnownState]}
          />
          <CardTitle className="truncate text-sm font-semibold">{label}</CardTitle>
          {machine?.region && (
            <span className="shrink-0 text-xs" title={machine.region}>
              {machine.region}
            </span>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
          {updateAvailable && (
            <span
              className="rounded-full border border-sky-500/25 bg-sky-500/10 px-2 py-0.5 text-[11px] font-normal text-sky-700 dark:text-sky-300"
              title={
                agentVersion
                  ? `采集端当前版本 ${agentVersion}，有可用更新（非故障）`
                  : '采集端有可用更新（非故障）'
              }
              data-agent-update-badge
            >
              可更新
            </span>
          )}
          {qualityMeta.label && (
            <span
              className={cn(
                'rounded-full px-2 py-0.5 text-[11px] font-medium',
                qualityMeta.badgeClass
              )}
              data-quality-badge={dataQuality}
            >
              {qualityMeta.label}
            </span>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-2 p-3 pt-0">
        {/* ---- 关键读数区：CPU / 内存 / 硬盘（source-failed 时降级展示最后已知值） ---- */}
        <div
          className={cn(
            'grid grid-cols-3 gap-1.5',
            dataQuality === 'source-failed' && 'opacity-75'
          )}
        >
          <OverviewMetricItem icon={Cpu} label="CPU" display={cpuDisplay} />
          <OverviewMetricItem icon={MemoryStick} label="内存" display={memDisplay} />
          <OverviewMetricItem icon={HardDrive} label="硬盘" display={diskDisplay} />
        </div>

        {/* ---- 紧凑趋势：24h 迷你图（传入 trend 时渲染，缺失历史不画假趋势） ---- */}
        {trend !== undefined && <CompactTrendSparkline trend={trend} quality={dataQuality} />}

        {/* ---- 底部新鲜度与状态说明行（B1-2 §4.2 / §6） ---- */}
        <div className="flex items-center justify-between gap-2 pt-0.5 text-[11px] text-muted-foreground">
          <div className="min-w-0 truncate">
            {dataQuality === 'source-failed' ? (
              <span>
                显示最后已知数据
                {sampledAt ? (
                  <>
                    {' · 截至 '}
                    <DisplayTime value={sampledAt} assumeUtc minute />
                  </>
                ) : null}
              </span>
            ) : dataQuality === 'stale' ? (
              <span>
                最后更新{' '}
                {sampledAt ? <DisplayTime value={sampledAt} assumeUtc relative /> : '—'}
              </span>
            ) : dataQuality === 'never-collected' ? (
              <span>暂无采样记录</span>
            ) : sampledAt ? (
              <span>
                采样 <DisplayTime value={sampledAt} assumeUtc relative />
              </span>
            ) : (
              <span>{ LAST_KNOWN_LABELS[lastKnownState] }</span>
            )}
          </div>
          {isInteractive && (
            <span className="inline-flex shrink-0 items-center gap-0.5 text-[11px] text-muted-foreground">
              <span>详情</span>
              <ArrowUpRight className="h-3 w-3" />
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );

  if (targetHref) {
    return (
      <Link
        to={targetHref}
        onClick={onClick}
        className="block rounded-lg transition-all hover:ring-2 hover:ring-primary/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {cardInner}
      </Link>
    );
  }

  return cardInner;
}
