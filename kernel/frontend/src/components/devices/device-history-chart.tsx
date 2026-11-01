import { useMemo, useRef, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DisplayTime } from '@/components/ui/display-time';
import { Skeleton } from '@/components/ui/skeleton';
import { formatGb, formatRate, statusTone } from '@/components/monitor/machine-card';
import { displayTime } from '@/lib/display-time';
import {
  DATA_QUALITY_META,
  resolveDataQuality,
  resolveLastKnownState,
  resolveMachineId,
  type HistoryGap,
  type HistoryPoint,
  type MachineOverviewSnapshot,
} from '@/components/dashboard/machine-overview-card';
import { cn } from '@/lib/utils';
import type { Machine, MonitorEvent } from '@/lib/types';

// ---------------------------------------------------------------------------
// B3-2 历史曲线与事件标记（总计划 B3；契约依据 B1-3 §3/§6、B1-1 §5.4）
//
// 纯展示组件：历史响应（B1-3 §6.2 形状）与事件列表由调用方传入（B3-1 单机页
// 负责取数与接线），本组件只消费、不请求。多序列共用同一时间轴（统一时间窗），
// hover 时全泳道同步显示对应时间点读数（曲线联动），事件在独立泳道按区间条
// 标记并与游标联动（事件标记）。
// ---------------------------------------------------------------------------

/** B1-3 §6.2 series 条目的前端消费形状（点/缺口类型复用 B2-2 已冻结定义）。 */
export interface DeviceHistorySeriesInput {
  metric: string;
  /** net_* 系列必须携带（B1-3 §6.2） */
  nic?: string;
  /** 覆盖默认中文标签的展示名（调用方有更具体语境时用） */
  label?: string;
  unit?: string;
  /** 系列实际采样间隔秒，默认 300（B1-3 §2.2 raw 档） */
  interval_sec?: number;
  points: HistoryPoint[];
  gaps?: HistoryGap[];
}

export interface DeviceHistoryWindowInput {
  start: string;
  end: string;
  effective_end?: string;
}

export interface DeviceHistoryInput {
  /** 实际使用的档位（`auto` 已解析，B1-3 §6.2） */
  resolution?: 'raw' | 'hourly';
  window?: DeviceHistoryWindowInput;
  series: DeviceHistorySeriesInput[];
}

export interface DeviceHistoryChartProps {
  /** 台账机器对象；身份与状态点用（与 B2-2 相同的解析规则） */
  machine?: Pick<Machine, 'id' | 'label' | 'role' | 'probe_status'> | null;
  /** B1 MachineSnapshot（兼容现有 MonitorSystem）；驱动头部健康/更新徽章 */
  snapshot?: MachineOverviewSnapshot | null;
  /** B1-3 历史查询响应；null = 未取到，渲染显式空态而非假曲线 */
  history?: DeviceHistoryInput | null;
  /** 事件列表（现有 MonitorEvent 形状）；时间落在窗口外的事件不渲染刻度 */
  events?: MonitorEvent[];
  loading?: boolean;
  className?: string;
}

// ---------------------------------------------------------------------------
// 纯函数（导出供 B3-1 接线与单测复用）
// ---------------------------------------------------------------------------

/** B1-3 §6.1 指标枚举的中文标签；未登记的 metric 原样显示（契约对指标名正交）。 */
const METRIC_LABELS: Record<string, string> = {
  cpu: 'CPU',
  mem_pct: '内存',
  mem_used: '内存已用',
  mem_total: '内存总量',
  disk_pct: '磁盘',
  disk_used: '磁盘已用',
  disk_total: '磁盘总量',
  load1: '负载 1m',
  load5: '负载 5m',
  load15: '负载 15m',
  net_up: '上行',
  net_down: '下行',
  net_sent_total: '累计发送',
  net_recv_total: '累计接收',
  temp: '温度',
  swap_used: 'Swap',
};

/** B1-3 §3.1 缺口原因的官方中文（契约表格原文）。 */
export const GAP_REASON_LABELS: Record<HistoryGap['reason'], string> = {
  no_samples: '未采到',
  retention_exceeded: '已过期',
  archived: '已归档',
  unsupported: '不支持',
};

/**
 * 分辨率自动档位（B1-3 §2.2）：跨度 ≤ 24h → raw（288 点 @5m），> 24h → hourly。
 * 请求侧的档位选择归数据获取层（B3-1 接线），此处只提供与契约一致的规则函数。
 */
export function pickResolution(spanMs: number): 'raw' | 'hourly' {
  return spanMs <= 24 * 3600 * 1000 ? 'raw' : 'hourly';
}

export const RESOLUTION_LABELS: Record<'raw' | 'hourly', string> = {
  raw: '原始采样',
  hourly: '小时聚合',
};

/** 累计计数器序列（net_sent_total / net_recv_total 等）：相邻点变小 = reset，必须断线（B1-3 §6.4）。 */
function isCounterMetric(metric: string): boolean {
  return /_total$/.test(metric);
}

export interface NormalizedPoint {
  ts: number;
  value: number;
}

/** 归一化点：兼容 B1-3 `{ts,value}` 与 B1-1 §4.3 `{t,v}`；null/非有限值直接丢弃，不补 0。 */
function normalizePoint(p: HistoryPoint): NormalizedPoint | null {
  const rawTs = 'ts' in p ? p.ts : p.t;
  const rawVal = 'value' in p ? p.value : p.v;
  if (rawVal == null || !Number.isFinite(rawVal)) return null;
  const ms = Date.parse(rawTs);
  if (!Number.isFinite(ms)) return null;
  return { ts: ms, value: rawVal };
}

export interface SeriesSegments {
  /** 断线后的连续段；每段内才连线 */
  segments: NormalizedPoint[][];
  /** 显式缺口区间（毫秒） */
  gaps: Array<{ start: number; end: number; reason: HistoryGap['reason'] }>;
  /** 计数器 reset 断线次数（reset 不是缺口，单列出来给 title 用） */
  resets: number;
  min: number;
  max: number;
}

/**
 * 序列分割（B1-3 §6.4 渲染规则）：点与缺口不相交；显式 gap、超过 2.5×interval
 * （对应 12 分钟新鲜度阈值的采样断更）、计数器变小（reset 锯齿）三种情况断线。
 * 不补 0、不插值、不前向填充——段与段之间留白，缺口用显式区间表达。
 */
export function splitSeriesSegments(
  series: DeviceHistorySeriesInput
): SeriesSegments {
  const normalized = (series.points || [])
    .map(normalizePoint)
    .filter((p): p is NormalizedPoint => p !== null)
    .sort((a, b) => a.ts - b.ts);

  const gaps = (series.gaps || [])
    .map((g) => ({
      start: Date.parse(g.start),
      end: Date.parse(g.end),
      reason: g.reason,
    }))
    .filter((g) => Number.isFinite(g.start) && Number.isFinite(g.end) && g.end > g.start);

  const values = normalized.map((p) => p.value);
  const min = values.length > 0 ? Math.min(...values) : 0;
  const max = values.length > 0 ? Math.max(...values) : 0;

  if (normalized.length === 0) {
    return { segments: [], gaps, resets: 0, min, max };
  }

  const intervalMs = Math.max(60, series.interval_sec ?? 300) * 1000;
  const maxStepMs = intervalMs * 2.5;
  const counter = isCounterMetric(series.metric);

  const segments: NormalizedPoint[][] = [];
  let resets = 0;
  let current: NormalizedPoint[] = [normalized[0]];

  for (let i = 1; i < normalized.length; i += 1) {
    const pt = normalized[i];
    const prev = normalized[i - 1];
    const hitsExplicitGap = gaps.some((g) => g.start < pt.ts && g.end > prev.ts);
    const exceedsStep = pt.ts - prev.ts > maxStepMs;
    // B1-3 §6.4：计数器变小（重启/重装归零）必须断线——不算缺口、不画下降斜线
    const counterReset = counter && pt.value < prev.value;
    if (counterReset) resets += 1;
    if (hitsExplicitGap || exceedsStep || counterReset) {
      segments.push(current);
      current = [pt];
    } else {
      current.push(pt);
    }
  }
  segments.push(current);

  return { segments, gaps, resets, min, max };
}

/**
 * 保形抽稀（B1-3 §6.4：1 年 hourly 8.7k 点可抽稀，抽稀不得造中间值）：
 * 按序分桶，每桶只输出桶内真实的最大与最小两个点——只做选择，不做任何
 * 插值/均值，抽稀后的每个点都是原始样本。
 */
export function thinPoints(points: NormalizedPoint[], maxPoints: number): NormalizedPoint[] {
  if (points.length <= maxPoints || points.length < 2) return points;
  const buckets = Math.max(2, Math.floor(maxPoints / 2));
  const size = points.length / buckets;
  const out: NormalizedPoint[] = [];
  let last: NormalizedPoint | null = null;
  const push = (p: NormalizedPoint) => {
    if (p !== last) {
      out.push(p);
      last = p;
    }
  };
  for (let b = 0; b < buckets; b += 1) {
    const from = Math.floor(b * size);
    const to = Math.min(points.length, Math.floor((b + 1) * size));
    if (from >= to) continue;
    let lo = points[from];
    let hi = points[from];
    for (let i = from + 1; i < to; i += 1) {
      if (points[i].value < lo.value) lo = points[i];
      if (points[i].value > hi.value) hi = points[i];
    }
    // 保时间序输出（min/max 谁先出现谁先走，抽稀不改变顺序语义）
    if (lo.ts <= hi.ts) {
      push(lo);
      push(hi);
    } else {
      push(hi);
      push(lo);
    }
  }
  return out;
}

/** 按系列单位格式化读数；不认识的单位保 2 位小数原样展示。 */
export function formatSeriesValue(value: number, unit?: string): string {
  const u = (unit || '').toLowerCase();
  if (u === '%' || u === 'percent') return `${value.toFixed(1)}%`;
  if (u === 'bytes/s') return formatRate(value);
  if (u === 'bytes') return `${(value / (1024 ** 3)).toFixed(2)} GiB`;
  if (u === 'gb') return formatGb(value);
  if (u === '°c') return `${value.toFixed(0)} °C`;
  if (u === 'count' || u === '') return `${Number(value.toFixed(2))}`;
  return `${Number(value.toFixed(2))} ${unit}`;
}

function seriesDisplayName(s: DeviceHistorySeriesInput): string {
  const base = s.label || METRIC_LABELS[s.metric] || s.metric;
  return s.nic ? `${base} · ${s.nic}` : base;
}

// ---------------------------------------------------------------------------
// 布局常量与坐标映射
// ---------------------------------------------------------------------------

const VIEW_W = 600;
const LANE_H = 48;
const LANE_PAD_Y = 5;
const EVENT_LANE_H = 18;
/** 抽稀目标：每泳道 viewBox 600 宽、每像素 2 点足够保形（1 年 hourly 8.7k → 1200）。 */
const MAX_POINTS_PER_LANE = VIEW_W * 2;
/** 分泳道以位置和名称识别指标，统一蓝色不随排序变化；不占用告警色。 */
const SERIES_COLOR = 'text-sky-600';

interface TimeDomain {
  minTs: number;
  maxTs: number;
  spanMs: number;
}

function toXPercent(ts: number, domain: TimeDomain): number {
  return ((ts - domain.minTs) / domain.spanMs) * 100;
}

/**
 * 时间域：优先用响应 window（effective_end 被 end 截断时以请求 end 为轴、
 * 截断只作 title 提示，避免「服务端截断」把轴拉短导致跨请求对比错位）；
 * 无 window 时退回全部点与事件时间的并集。
 */
function buildTimeDomain(
  history: DeviceHistoryInput | null | undefined,
  eventTimes: number[]
): TimeDomain | null {
  const win = history?.window;
  const candidates: number[] = [];
  if (win?.start && Number.isFinite(Date.parse(win.start))) candidates.push(Date.parse(win.start));
  if (win?.end && Number.isFinite(Date.parse(win.end))) candidates.push(Date.parse(win.end));
  if (candidates.length >= 2) {
    const minTs = Math.min(...candidates);
    const maxTs = Math.max(...candidates);
    if (maxTs > minTs) return { minTs, maxTs, spanMs: maxTs - minTs };
  }
  const pointTimes = (history?.series || []).flatMap((s) =>
    (s.points || []).map((p) => {
      const raw = 'ts' in p ? p.ts : p.t;
      const ms = Date.parse(raw);
      return Number.isFinite(ms) ? ms : Number.NaN;
    })
  );
  const all = [...pointTimes, ...eventTimes].filter((n) => Number.isFinite(n));
  if (all.length < 2) return null;
  const minTs = Math.min(...all);
  const maxTs = Math.max(...all);
  if (maxTs <= minTs) return null;
  return { minTs, maxTs, spanMs: maxTs - minTs };
}

// ---------------------------------------------------------------------------
// 事件泳道
// ---------------------------------------------------------------------------

interface EventBar {
  event: MonitorEvent;
  startTs: number;
  endTs: number;
  /** 是否仍在进行（未恢复）：条延伸到窗口尾缘 */
  active: boolean;
}

/** 事件区间条：opened_at → resolved_at（未恢复则到轴尾缘）。窗口外事件不渲染。 */
function buildEventBars(events: MonitorEvent[], domain: TimeDomain): EventBar[] {
  const bars: EventBar[] = [];
  for (const event of events || []) {
    const startTs = Date.parse(event.opened_at);
    if (!Number.isFinite(startTs) || startTs > domain.maxTs) continue;
    const resolvedTs = event.resolved_at ? Date.parse(event.resolved_at) : Number.NaN;
    const active = !Number.isFinite(resolvedTs);
    const endTs = active ? domain.maxTs : resolvedTs;
    if (endTs < domain.minTs) continue;
    bars.push({
      event,
      startTs: Math.max(startTs, domain.minTs),
      endTs: Math.min(endTs, domain.maxTs),
      active,
    });
  }
  return bars;
}

function eventBarTitle(bar: EventBar): string {
  const state = bar.active ? '未恢复' : '已恢复';
  const dur = bar.event.duration_sec != null ? `，持续 ${Math.round(bar.event.duration_sec / 60)} 分钟` : '';
  return `${bar.event.title}（${state}${dur}）`;
}

// ---------------------------------------------------------------------------
// hover 联动模型
// ---------------------------------------------------------------------------

interface HoverSample {
  seriesIndex: number;
  label: string;
  colorClass: string;
  value: number;
  unit?: string;
  point: NormalizedPoint;
}

/**
 * 游标取值：每个序列找距游标时间最近的点，容差 0.75×interval——超过一个
 * 采样周期的缺席就是断线/缺口，不显示「最近点」冒充当前值（不插值原则
 * 在交互层的延伸）。
 */
function sampleAt(hoverTs: number, models: SeriesModel[]): HoverSample[] {
  const out: HoverSample[] = [];
  models.forEach((m, seriesIndex) => {
    if (!m.points.length) return;
    let best: NormalizedPoint | null = null;
    let bestDist = Infinity;
    for (const p of m.points) {
      const dist = Math.abs(p.ts - hoverTs);
      if (dist < bestDist) {
        bestDist = dist;
        best = p;
      }
    }
    if (!best) return;
    const intervalMs = Math.max(60, m.series.interval_sec ?? 300) * 1000;
    if (bestDist > intervalMs * 0.75) return;
    out.push({
      seriesIndex,
      label: m.label,
      colorClass: m.colorClass,
      value: best.value,
      unit: m.series.unit,
      point: best,
    });
  });
  return out;
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

interface SeriesModel {
  series: DeviceHistorySeriesInput;
  label: string;
  colorClass: string;
  /** 抽稀后的全量点（hover 取值用；抽稀只做选择不做插值，见 thinPoints） */
  points: NormalizedPoint[];
  segments: NormalizedPoint[][];
  gaps: Array<{ start: number; end: number; reason: HistoryGap['reason'] }>;
  resets: number;
  unsupported: boolean;
  min: number;
  max: number;
}

const LAST_KNOWN_LABELS: Record<string, string> = {
  up: '在线',
  down: '离线',
  unknown: '状态未知',
};

export function DeviceHistoryChart({
  machine,
  snapshot,
  history,
  events,
  loading = false,
  className,
}: DeviceHistoryChartProps) {
  const plotRef = useRef<HTMLDivElement | null>(null);
  const [hoverPct, setHoverPct] = useState<number | null>(null);

  // ---- 头部状态徽章（B1-1 §5.4：健康用 signal 语义色，更新提示用 sky 信息色，两组视觉严格分离）----
  const machineId = resolveMachineId(machine, snapshot);
  const lastKnownState = resolveLastKnownState(machine, snapshot);
  const dataQuality = resolveDataQuality(snapshot);
  const qualityMeta = DATA_QUALITY_META[dataQuality];
  const updateAvailable = snapshot?.agent_update_available === true;
  const agentVersion = snapshot?.agent_version ?? null;

  // ---- 时间域与泳道模型 ----
  const eventTimes = useMemo(
    () =>
      (events || [])
        .map((e) => Date.parse(e.opened_at))
        .filter((n) => Number.isFinite(n)),
    [events]
  );
  const domain = useMemo(() => buildTimeDomain(history, eventTimes), [history, eventTimes]);

  const models = useMemo<SeriesModel[]>(() => {
    // 分割与 domain 无关（空窗/全 unsupported 也要能显式表达，B1-1 §6 能力/质量不同词）
    return (history?.series || []).map((series) => {
      const split = splitSeriesSegments(series);
      const allPoints = split.segments.flat();
      const unsupported = (series.gaps || []).some((g) => g.reason === 'unsupported');
      return {
        series,
        label: seriesDisplayName(series),
        colorClass: SERIES_COLOR,
        points: thinPoints(allPoints, MAX_POINTS_PER_LANE),
        segments: split.segments.map((seg) => thinPoints(seg, MAX_POINTS_PER_LANE)),
        gaps: split.gaps,
        resets: split.resets,
        unsupported,
        min: split.min,
        max: split.max,
      };
    });
  }, [history]);

  // domain 为 null 时 models 仍可能非空（如全 unsupported 无点的序列），
  // 但没有时间轴就没有可画的泳道——统一走空态分支表达。
  const hasAnyData = Boolean(domain) && models.some((m) => m.points.length > 0);

  const eventBars = useMemo(
    () => (domain ? buildEventBars(events || [], domain) : []),
    [events, domain]
  );
  const truncatedEnd =
    history?.window?.effective_end &&
    history.window.end &&
    history.window.effective_end !== history.window.end;

  // ---- hover / 键盘游标（曲线联动：单一 hoverPct 驱动全部泳道）----
  const hoverTs =
    hoverPct != null && domain ? domain.minTs + (hoverPct / 100) * domain.spanMs : null;
  const hoverSamples = hoverTs != null ? sampleAt(hoverTs, models) : [];
  const hoverEvents =
    hoverTs != null
      ? eventBars.filter((b) => hoverTs >= b.startTs && hoverTs <= b.endTs)
      : [];

  const handlePointerMove = (clientX: number) => {
    const rect = plotRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    const pct = ((clientX - rect.left) / rect.width) * 100;
    setHoverPct(Math.min(100, Math.max(0, pct)));
  };

  const nudgeCursor = (deltaPct: number) => {
    setHoverPct((prev) => {
      const base = prev ?? 50;
      return Math.min(100, Math.max(0, base + deltaPct));
    });
  };

  // ---- 渲染 ----
  return (
    <Card className={className} data-device-history-chart data-machine={machineId || undefined}>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0 p-3 pb-2">
        <CardTitle className="text-base">历史曲线</CardTitle>
        <div className="flex flex-wrap items-center gap-1.5">
          {/* 健康区：运行状态点 + 五态质量徽章（signal 语义色，B1-1 §5.1/§5.2） */}
          <span
            className={cn('h-2.5 w-2.5 shrink-0 rounded-full', statusTone(lastKnownState))}
            title={LAST_KNOWN_LABELS[lastKnownState]}
            aria-label={LAST_KNOWN_LABELS[lastKnownState]}
          />
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
          {/* 更新提示区：sky 信息色徽章，与故障视觉严格分离（B1-1 §5.4——更新不是故障） */}
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
          {agentVersion && !updateAvailable && (
            <span
              className="rounded-full border border-sky-500/25 bg-sky-500/5 px-2 py-0.5 text-[11px] font-normal text-sky-700/80 dark:text-sky-300/80"
              title={`采集端版本 ${agentVersion}`}
            >
              v{agentVersion}
            </span>
          )}
          {/* 分辨率档位（B1-3 §2.2：raw ≤24h / hourly >24h，响应回显实际档位） */}
          {history?.resolution && (
            <span
              className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
              title={`实际分辨率档位：${RESOLUTION_LABELS[history.resolution]}${
                truncatedEnd ? '；窗口终点已按服务器当前时间截断' : ''
              }`}
              data-resolution={history.resolution}
            >
              {RESOLUTION_LABELS[history.resolution]}
            </span>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-2 p-3 pt-0">
        {loading ? (
          <div className="space-y-2" data-history-loading>
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-4 w-2/3" />
          </div>
        ) : !domain || (!hasAnyData && eventBars.length === 0) ? (
          /* 空态显式表达：全 unsupported 与「暂无样本」不同词（B1-1 §6 能力/质量永不同词） */
          <div
            className="flex items-center justify-between rounded-md border border-dashed border-border/70 px-3 py-3 text-xs text-muted-foreground"
            data-history-state={models.some((m) => m.unsupported) ? 'unsupported' : 'empty'}
          >
            <span>所选时间窗内暂无历史样本</span>
            <span>缺口不补 0、不插值</span>
          </div>
        ) : (
          <div className="space-y-0.5">
            {/* ---- tooltip：游标时间 + 各序列读数 + 命中事件（联动汇总）---- */}
            {hoverTs != null && (
              <div
                className="relative h-0"
                aria-hidden
              >
                <div
                  className={cn('pointer-events-none absolute bottom-1 z-10 max-w-[min(20rem,100%)] rounded-md border bg-popover px-2.5 py-1.5 text-popover-foreground shadow-md',
                    (hoverPct ?? 50) >= 75 ? '-translate-x-full' : (hoverPct ?? 50) > 25 ? '-translate-x-1/2' : '')}
                  style={{ left: `${(hoverPct ?? 50) >= 75 ? 100 : (hoverPct ?? 50) > 25 ? 50 : 0}%` }}
                  data-history-tooltip
                >
                  <p className="whitespace-nowrap font-mono text-[11px] tabular-nums text-muted-foreground">
                    <DisplayTime value={new Date(hoverTs).toISOString()} assumeUtc minute />
                  </p>
                  {hoverSamples.length === 0 && hoverEvents.length === 0 && (
                    <p className="text-xs text-muted-foreground">该时刻无样本（缺口）</p>
                  )}
                  {hoverSamples.map((s) => (
                    <p key={s.seriesIndex} className="flex items-center gap-1.5 text-xs">
                      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full bg-current', s.colorClass)} />
                      <span className="truncate">{s.label}</span>
                      <span className="ml-auto font-mono tabular-nums">
                        {formatSeriesValue(s.value, s.unit)}
                      </span>
                    </p>
                  ))}
                  {hoverEvents.slice(0, 3).map((b) => (
                    <p key={b.event.id} className="flex items-center gap-1.5 text-[11px]">
                      <span
                        className={cn(
                          'h-1.5 w-1.5 shrink-0 rounded-full bg-current',
                          b.event.severity === 'error' ? 'text-signal-5' : 'text-signal-4'
                        )}
                      />
                      <span className="truncate text-muted-foreground">{b.event.title}</span>
                      {b.active && <span className="text-muted-foreground">（未恢复）</span>}
                    </p>
                  ))}
                </div>
              </div>
            )}

            {/* ---- 绘图区：每序列一条泳道（统一时间轴），底部事件泳道 ---- */}
            <div
              className="relative touch-pan-y"
              tabIndex={0}
              role="application"
              aria-label={`历史曲线图，共 ${models.length} 条曲线与 ${eventBars.length} 个事件标记，左右方向键移动时间游标`}
              data-history-plot
              onMouseMove={(e) => handlePointerMove(e.clientX)}
              onMouseLeave={() => setHoverPct(null)}
              onTouchStart={(e) => handlePointerMove(e.touches[0].clientX)}
              onTouchMove={(e) => handlePointerMove(e.touches[0].clientX)}
              onTouchEnd={() => setHoverPct(null)}
              onKeyDown={(e) => {
                if (e.key === 'ArrowLeft') {
                  e.preventDefault();
                  nudgeCursor(-100 / 60);
                } else if (e.key === 'ArrowRight') {
                  e.preventDefault();
                  nudgeCursor(100 / 60);
                } else if (e.key === 'Home') {
                  e.preventDefault();
                  setHoverPct(0);
                } else if (e.key === 'End') {
                  e.preventDefault();
                  setHoverPct(100);
                }
              }}
            >
              {/* 游标线：贯穿全部泳道（曲线联动的视觉锚） */}
              {hoverPct != null && (
                <div className="pointer-events-none absolute bottom-0 left-[6rem] right-0 top-0 sm:left-[8rem]">
                  <div
                    className="absolute bottom-0 top-0 z-[5] w-px bg-foreground/30"
                    style={{ left: `${hoverPct}%` }}
                    data-history-cursor
                  />
                </div>
              )}

              <div ref={plotRef} className="pointer-events-none absolute bottom-0 left-[6rem] right-0 top-0 sm:left-[8rem]" data-history-time-domain />
              {models.map((m, index) => (
                <LaneRow
                  key={`${m.series.metric}-${m.series.nic ?? ''}-${index}`}
                  model={m}
                  domain={domain}
                  hoverPct={hoverPct}
                  hoverPoint={
                    hoverSamples.find((s) => s.seriesIndex === index)?.point ?? null
                  }
                />
              ))}

              {/* 事件泳道：区间条按 severity 着色（signal 语义色） */}
              {eventBars.length > 0 && (
                <div className="relative ml-[6rem] sm:ml-[8rem]" data-event-lane>
                  <div className="absolute -top-0.5 left-0 text-[10px] leading-none text-muted-foreground">
                    事件
                  </div>
                  <div
                    className="relative"
                    style={{ height: EVENT_LANE_H }}
                  >
                    {eventBars.map((bar) => {
                      const left = toXPercent(bar.startTs, domain);
                      const right = toXPercent(bar.endTs, domain);
                      return (
                        <div
                          key={bar.event.id}
                          className={cn(
                            'absolute top-1/2 h-1.5 -translate-y-1/2 rounded-sm',
                            bar.event.severity === 'error' ? 'bg-signal-5/70' : 'bg-signal-4/70',
                            bar.active && 'animate-pulse'
                          )}
                          style={{
                            left: `${left}%`,
                            width: `${Math.max(0.5, right - left)}%`,
                          }}
                          title={eventBarTitle(bar)}
                          data-event-bar={bar.event.id}
                        />
                      );
                    })}
                  </div>
                </div>
              )}

              {/* 时间轴刻度（全部泳道共用） */}
              <TimeAxisRow domain={domain} />
            </div>
          </div>
        )}
        {!loading && models.length > 0 && (
          <details className="text-xs text-muted-foreground">
            <summary className="cursor-pointer py-1">查看样本与缺口</summary>
            <div className="max-h-64 overflow-auto">
              <table className="w-full text-left text-xs">
                <caption className="sr-only">历史样本与缺口明细</caption>
                <thead><tr><th>指标</th><th>时间或区间</th><th>读数或原因</th></tr></thead>
                <tbody>{models.flatMap((model) => [
                  ...(model.series.points || []).map((point, i) => {
                    const normalized = normalizePoint(point);
                    return normalized && <tr key={`${model.label}-point-${i}`}>
                      <td>{model.label}</td><td><DisplayTime value={new Date(normalized.ts).toISOString()} /></td>
                      <td className="font-mono">{formatSeriesValue(normalized.value, model.series.unit)}</td>
                    </tr>;
                  }),
                  ...model.gaps.map((gap, i) => <tr key={`${model.label}-gap-${i}`}>
                    <td>{model.label}</td><td><DisplayTime value={new Date(gap.start).toISOString()} /> → <DisplayTime value={new Date(gap.end).toISOString()} /></td>
                    <td>{GAP_REASON_LABELS[gap.reason]}</td>
                  </tr>),
                ])}</tbody>
              </table>
            </div>
          </details>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 子组件
// ---------------------------------------------------------------------------

function LaneRow({
  model,
  domain,
  hoverPct,
  hoverPoint,
}: {
  model: SeriesModel;
  domain: TimeDomain;
  hoverPct: number | null;
  hoverPoint: NormalizedPoint | null;
}) {
  const { series } = model;

  if (model.unsupported && model.points.length === 0) {
    return (
      <div
        className="flex h-8 items-center justify-between rounded-md border border-dashed border-border/70 px-2 text-[11px] text-muted-foreground"
        data-lane-state="unsupported"
      >
        <span>{model.label}</span>
        <span>该机不支持此指标</span>
      </div>
    );
  }
  if (model.points.length === 0) {
    return (
      <div
        className="flex h-8 items-center justify-between rounded-md border border-dashed border-border/70 px-2 text-[11px] text-muted-foreground"
        data-lane-state="empty"
      >
        <span>{model.label}</span>
        <span>暂无样本</span>
      </div>
    );
  }

  // y 域：百分比指标固定 0–100（利用率语义固定域，跨刷新不跳变）；
  // 其余按数据域 pad 10%，下限不强制 0（负载/温度的 0 无意义占位）。
  const isPercent = (series.unit || '') === '%' || (series.unit || '') === 'percent';
  const valSpan = isPercent
    ? { min: 0, max: 100 }
    : {
        min: model.min - (model.max - model.min || 1) * 0.1,
        max: model.max + (model.max - model.min || 1) * 0.1,
      };
  const valRange = Math.max(1e-6, valSpan.max - valSpan.min);

  const toX = (ts: number) => ((ts - domain.minTs) / domain.spanMs) * VIEW_W;
  const toY = (v: number) =>
    LANE_PAD_Y +
    (LANE_H - LANE_PAD_Y * 2) *
      (1 - Math.min(1, Math.max(0, (v - valSpan.min) / valRange)));

  const coordPairs = (pts: NormalizedPoint[]) =>
    pts.map((p) => `${toX(p.ts).toFixed(2)},${toY(p.value).toFixed(2)}`).join(' ');

  const latest = model.points[model.points.length - 1];
  const gapCount = model.gaps.length;

  return (
    <div className="flex items-center" data-lane={model.label}>
      {/* 左侧固定标签列：色点 + 名称 + 最新值（读数不进 SVG，避免 none 拉伸下的文字变形） */}
      <div
        className="flex w-[6rem] shrink-0 flex-col gap-0.5 pr-2 sm:w-[8rem]"
        title={`${model.label}（间隔 ${Math.max(60, series.interval_sec ?? 300)}s${
          gapCount > 0 ? `，缺口 ${gapCount} 处` : ''
        }${model.resets > 0 ? `，计数器归零 ${model.resets} 次` : ''}）`}
      >
        <span className="flex min-w-0 items-center gap-1 text-[11px] leading-none text-muted-foreground">
          <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full bg-current', model.colorClass)} />
          <span className="truncate">{model.label}</span>
        </span>
        <span className="pl-2.5 font-mono text-[11px] leading-none tabular-nums">
          {formatSeriesValue(latest.value, series.unit)}
        </span>
      </div>

      {/* 泳道主体：preserveAspectRatio="none" 随容器宽拉伸（B2-2 迷你图同款模式） */}
      <div className="relative min-w-0 flex-1">
        <svg
          viewBox={`0 0 ${VIEW_W} ${LANE_H}`}
          className="block h-12 w-full"
          preserveAspectRatio="none"
          role="img"
          aria-label={`${model.label}历史曲线`}
        >
          {/* 缺口区间：显式浅色底 + SVG title 说明原因（不连线、不补值） */}
          {model.gaps.map((g, i) => {
            const x1 = Math.max(0, toX(Math.max(g.start, domain.minTs)));
            const x2 = Math.min(VIEW_W, toX(Math.min(g.end, domain.maxTs)));
            if (x2 <= x1) return null;
            return (
              <rect
                key={`gap-${i}`}
                x={x1}
                y={0}
                width={x2 - x1}
                height={LANE_H}
                className="fill-muted-foreground/10"
                data-gap-reason={g.reason}
              >
                <title>{`${GAP_REASON_LABELS[g.reason]}：${g.start} ~ ${g.end}`}</title>
              </rect>
            );
          })}
          {/* 百分比泳道画两条参考线（70/90 与用量色带阈值同源，machine-card meterTone） */}
          {isPercent && (
            <>
              <line
                x1={0}
                x2={VIEW_W}
                y1={toY(70)}
                y2={toY(70)}
                className="stroke-muted-foreground/15"
                strokeWidth={1}
                strokeDasharray="4 4"
              />
              <line
                x1={0}
                x2={VIEW_W}
                y1={toY(90)}
                y2={toY(90)}
                className="stroke-signal-5/20"
                strokeWidth={1}
                strokeDasharray="4 4"
              />
            </>
          )}
          {model.segments.map((seg, i) =>
            seg.length >= 2 ? (
              <polyline
                key={`seg-${i}`}
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
                className={cn(model.colorClass, 'opacity-80')}
                points={coordPairs(seg)}
                vectorEffect="non-scaling-stroke"
              />
            ) : null
          )}
        </svg>

        {model.segments.filter((segment) => segment.length === 1).map(([point]) => (
          <span key={point.ts} className={cn('pointer-events-none absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-background bg-current', model.colorClass)}
            style={{ left: `${toXPercent(point.ts, domain)}%`, top: `${(toY(point.value) / LANE_H) * 100}%` }} data-history-single-point />
        ))}
        {/* hover 标记点：HTML 定位不受 none 拉伸影响，圆形不变形 */}
        {hoverPoint && hoverPct != null && (
          <span
            className={cn(
              'pointer-events-none absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-background bg-current',
              model.colorClass
            )}
            style={{
              left: `${toXPercent(hoverPoint.ts, domain)}%`,
              top: `${(toY(hoverPoint.value) / LANE_H) * 100}%`,
            }}
            data-lane-marker
          />
        )}
      </div>
    </div>
  );
}

function TimeAxisRow({ domain }: { domain: TimeDomain }) {
  const ticks = [0, 25, 50, 75, 100];
  return (
    <div className="relative ml-[6rem] h-8 border-t border-border/60 sm:ml-[8rem]" data-time-axis>
      {ticks.map((pct) => {
        // 轴刻度用浏览器本地时间（displayTime 同口径），title 保留 UTC 原值
        const iso = new Date(domain.minTs + (pct / 100) * domain.spanMs).toISOString();
        const local = displayTime(iso, { assumeUtc: true, minute: true }).text;
        return (
          <span
            key={pct}
            className={cn(
              'absolute top-0.5 -translate-x-1/2 whitespace-nowrap font-mono text-[10px] leading-none tabular-nums text-muted-foreground',
              pct === 0 && 'translate-x-0',
              pct === 100 && '-translate-x-full',
              pct > 0 && pct < 100 && 'hidden lg:block'
            )}
            style={{ left: `${pct}%` }}
            title={iso}
          >
            <span className="block">{local.slice(5, 10)}</span>
            <span className="block">{local.slice(11)}</span>
          </span>
        );
      })}
    </div>
  );
}
