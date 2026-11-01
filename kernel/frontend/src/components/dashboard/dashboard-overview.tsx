import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { AlertTriangle, ArrowUpRight, CheckCircle2, RefreshCw } from 'lucide-react';
import { ObservabilitySummary } from '@/components/monitor/observability-summary';
import {
   DATA_QUALITY_META,
  TODAY_FOCUS_GROUPS,
  TODAY_FOCUS_GROUP_ORDER,
  TodayFocus,
  buildTodayFocusGroups,
  computeMetricCoverage,
  normalizeTemperatureReading,
  resolveCapabilityStatus,
  resolveDataQuality,
  resolveLastKnownState,
  resolveMachineId,
} from './today-focus';
import type {
  DataQualityState,
  TodayFocusGroupKey,
  TodayFocusItem,
  TodayFocusSystem,
} from './today-focus';
import { MachineOverviewCard } from './machine-overview-card';
import type { CompactTrendSeries, MachineOverviewSnapshot } from './machine-overview-card';
import {
  ORPHAN_SYSTEM_PREFIX,
  buildDefaultLayout,
  machineWidgetId,
} from './layout-model';
import type { LayoutItem, WidgetKey } from './layout-model';
import { widgetLabel } from './hidden-model';
import { getAttention } from '@/lib/api';
import type { AttentionItem, Machine, ObservabilityPayload, Service } from '@/lib/types';
import { cn } from '@/lib/utils';

export interface DashboardOverviewProps {
  /** 已经过 `default ⊕ override` 合并并过滤隐藏项的 GridStack 布局模型 */
  layout?: LayoutItem[];
  machines: Machine[];
  systems: TodayFocusSystem[];
  observability?: ObservabilityPayload | null;
  monitorUnavailable?: boolean;
  renderWidget?: (key: WidgetKey) => ReactNode;
  labelOf?: (key: WidgetKey) => string;
  /** 可选：按台账 machine_id 传入 24h 紧凑趋势序列（B1-3 / B2-2），缺失历史不画假趋势 */
  trendsByMachine?: Record<string, CompactTrendSeries | null | undefined>;
  /** 可选：注入 `/api/attention` 关注项（未传时在关注区块可见时自动拉取） */
  attentionItems?: AttentionItem[];
  /** 可选：注入登记服务列表供证书到期归类 */
  services?: Service[];
  /** 可选：受控分组筛选（与今日关注分组联动） */
  activeFocusGroup?: TodayFocusGroupKey | null;
  onFocusGroupChange?: (group: TodayFocusGroupKey | null) => void;
}

interface OverviewMachineEntry {
  key: WidgetKey;
  rawWidgetId: string;
  canonicalId: string;
  label: string;
  layoutIndex: number;
  machine?: Machine;
  rawSystem?: TodayFocusSystem;
  snapshot: MachineOverviewSnapshot;
  dataQuality: DataQualityState;
  lastKnownState: 'up' | 'down' | 'unknown';
  isFault: boolean;
  isExpiring: boolean;
  isUpdate: boolean;
  faultPriority: number;
}

function parseFocusParam(raw: string | null): TodayFocusGroupKey | null {
  if (raw === 'fault' || raw === 'expiring' || raw === 'update') {
    return raw;
  }
  return null;
}

/**
 * 归一化单机 24h 趋势输入（B2 验收条款：缺失历史不画假趋势）。
 * - 当机器处于 `never-collected` 或无历史序列时返回 `null`，由卡片显式展示「暂无历史样本」
 * - 过滤掉非有限数值点，绝不将缺失点补 0 或插值伪造平滑曲线
 */
export function resolveOverviewTrend(
  canonicalId: string,
  trendsByMachine?: Record<string, CompactTrendSeries | null | undefined>,
  dataQuality?: DataQualityState,
): CompactTrendSeries | null {
  if (dataQuality === 'never-collected') {
    return null;
  }
  const raw = trendsByMachine?.[canonicalId] ?? null;
  if (!raw) {
    return null;
  }
  const validPoints = (raw.points || []).filter((pt) => {
    const val = 'value' in pt ? pt.value : pt.v;
    return val != null && Number.isFinite(val);
  });
  return {
    ...raw,
    points: validPoints,
  };
}

export function DashboardOverview({
  layout,
  machines,
  systems,
  observability = null,
  monitorUnavailable = false,
  renderWidget = () => null,
  labelOf,
  trendsByMachine,
  attentionItems: injectedAttentionItems,
  services,
  activeFocusGroup,
  onFocusGroupChange,
}: DashboardOverviewProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [fetchedAttention, setFetchedAttention] = useState<AttentionItem[]>([]);

  // 独立渲染兜底：调用方未传 layout 时按 GridStack 默认模型现算
  const effectiveLayout = useMemo(
    () => layout ?? buildDefaultLayout({ machines, systems }),
    [layout, machines, systems],
  );

  const resolveLabel = useMemo(
    () =>
      labelOf ??
      ((key: WidgetKey) => widgetLabel(key, { machines, systems })),
    [labelOf, machines, systems],
  );

  const hasAttention = useMemo(
    () => effectiveLayout.some((item) => item.key === 'attention'),
    [effectiveLayout],
  );
  const hasEvents = useMemo(
    () => effectiveLayout.some((item) => item.key === 'monitor-events'),
    [effectiveLayout],
  );
  const machineLayoutItems = useMemo(
    () => effectiveLayout.filter((item) => machineWidgetId(item.key) !== null),
    [effectiveLayout],
  );
  const detailItems = useMemo(
    () =>
      effectiveLayout.filter(
        (item) => item.key !== 'attention' && machineWidgetId(item.key) === null,
      ),
    [effectiveLayout],
  );

  // 与现有 AttentionStrip 对齐：当今日关注未隐藏且未外部注入时，非阻塞拉取后端关注项
  useEffect(() => {
    if (injectedAttentionItems !== undefined || !hasAttention) return;
    if (typeof getAttention !== 'function') return;
    let alive = true;
    Promise.resolve(getAttention())
      .then((items) => {
        if (alive && Array.isArray(items)) {
          setFetchedAttention(items);
        }
      })
      .catch(() => {
        // 失败不清空机器概况与本地推导的故障/到期/更新分组
      });
    return () => {
      alive = false;
    };
  }, [hasAttention, injectedAttentionItems]);

  const resolvedAttentionItems = injectedAttentionItems ?? fetchedAttention;

  // URL ?focus= 参数驱动筛选：刷新、浏览器返回、简览⇄完整总览切换均保持筛选状态
  const urlFocusGroup = parseFocusParam(searchParams.get('focus'));
  const activeGroup = activeFocusGroup !== undefined ? activeFocusGroup : urlFocusGroup;

  const handleSelectGroup = (nextGroup: TodayFocusGroupKey | null) => {
    if (onFocusGroupChange) {
      onFocusGroupChange(nextGroup);
    }
    const nextParams = new URLSearchParams(searchParams);
    if (nextGroup) {
      nextParams.set('focus', nextGroup);
    } else {
      nextParams.delete('focus');
    }
    setSearchParams(nextParams, { replace: true });
  };

  // 将 layout 中未被隐藏的机器项解析为统一概况模型（严格保留隐藏偏好与自定义布局索引）
  const baseEntries = useMemo(() => {
    return machineLayoutItems.map(({ key }, layoutIndex) => {
      const rawWidgetId = machineWidgetId(key)!;
      const isOrphan = rawWidgetId.startsWith(ORPHAN_SYSTEM_PREFIX);
      const orphanSysId = isOrphan ? rawWidgetId.slice(ORPHAN_SYSTEM_PREFIX.length) : null;

      const machine = isOrphan
        ? undefined
        : machines.find((item) => item.id.toLowerCase() === rawWidgetId.toLowerCase());
      const rawSystem = isOrphan
        ? systems.find((item) => item.id === orphanSysId)
        : systems.find((item) => resolveMachineId(item) === rawWidgetId.toLowerCase());

      const canonicalId = (
        machine?.id ||
        resolveMachineId(rawSystem, machine) ||
        orphanSysId ||
        rawWidgetId
      ).toLowerCase();

      const label = resolveLabel(key);
      const dataQuality = resolveDataQuality(machine, rawSystem, {
        sourceFailed: monitorUnavailable,
      });
      const lastKnownState = resolveLastKnownState(machine, rawSystem);
      const normalizedTemp = normalizeTemperatureReading(machine, rawSystem);

      const snapshot: MachineOverviewSnapshot = {
        ...(rawSystem ?? {}),
        machine_id: canonicalId,
        machine: rawSystem?.machine ?? machine?.id ?? canonicalId,
        name: rawSystem?.name ?? machine?.label ?? label,
        last_known_state: lastKnownState,
        data_quality: dataQuality,
        temperature: normalizedTemp ?? undefined,
        capabilities: {
          base: resolveCapabilityStatus('base', machine, rawSystem),
          containers: resolveCapabilityStatus('containers', machine, rawSystem),
          temperature: resolveCapabilityStatus('temperature', machine, rawSystem),
          systemd: resolveCapabilityStatus('systemd', machine, rawSystem),
          'package-updates': resolveCapabilityStatus('package-updates', machine, rawSystem),
          smart: resolveCapabilityStatus('smart', machine, rawSystem),
          'traffic-ledger': resolveCapabilityStatus('traffic-ledger', machine, rawSystem),
          ...(rawSystem?.capabilities ?? {}),
        },
      };

      return {
        key,
        rawWidgetId,
        canonicalId,
        label,
        layoutIndex,
        machine,
        rawSystem,
        snapshot,
        dataQuality,
        lastKnownState,
      };
    });
  }, [machineLayoutItems, machines, systems, monitorUnavailable, resolveLabel]);

  // 仅针对当前布局中可见（未隐藏）的设备集合计算今日关注与指标覆盖分母
  const visibleMachines = useMemo(
    () =>
      baseEntries
        .map((entry) => entry.machine)
        .filter((m): m is Machine => m !== undefined),
    [baseEntries],
  );

  const visibleSystems = useMemo(
    () =>
      baseEntries
        .map((entry) => entry.rawSystem)
        .filter((s): s is TodayFocusSystem => s !== undefined),
    [baseEntries],
  );

  const focusGroups = useMemo(
    () =>
      buildTodayFocusGroups({
        machines: visibleMachines.length > 0 || baseEntries.length > 0 ? visibleMachines : machines,
        systems: visibleSystems.length > 0 || baseEntries.length > 0 ? visibleSystems : systems,
        observability,
        services,
        attentionItems: resolvedAttentionItems,
        monitorUnavailable,
      }),
    [
      visibleMachines,
      visibleSystems,
      baseEntries.length,
      machines,
      systems,
      observability,
      services,
      resolvedAttentionItems,
      monitorUnavailable,
    ],
  );

  // B1-1 §8.3：统计分母按「实际有该指标有效读数的机器数」，不用全量机器数冒充
  const coverage = useMemo(
    () =>
      computeMetricCoverage(
        visibleMachines.length > 0 || baseEntries.length > 0 ? visibleMachines : machines,
        visibleSystems.length > 0 || baseEntries.length > 0 ? visibleSystems : systems,
        { sourceFailed: monitorUnavailable },
      ),
    [visibleMachines, visibleSystems, baseEntries.length, machines, systems, monitorUnavailable],
  );

  // 确保从「今日关注」异常计数点击筛选抵达同一批设备对象
  const groupMachineSets = useMemo(() => {
    const faultIds = new Set<string>();
    const expiringIds = new Set<string>();
    const updateIds = new Set<string>();

    for (const item of focusGroups.fault) {
      if (item.machineId) faultIds.add(item.machineId.toLowerCase());
    }
    for (const item of focusGroups.expiring) {
      if (item.machineId) expiringIds.add(item.machineId.toLowerCase());
    }
    for (const item of focusGroups.update) {
      if (item.machineId) updateIds.add(item.machineId.toLowerCase());
    }

    for (const entry of baseEntries) {
      if (entry.dataQuality !== 'fresh' || entry.lastKnownState === 'down') {
        faultIds.add(entry.canonicalId);
      }
      // B1-1 §5.4：agent_update_available 仅进更新集合，绝不混入故障集合
      if (entry.snapshot.agent_update_available === true) {
        updateIds.add(entry.canonicalId);
      }
    }

    return { faultIds, expiringIds, updateIds };
  }, [focusGroups, baseEntries]);

  // 组合完整设备条目并执行「异常优先 + 同级保留自定义布局顺序」排序
  const machineEntries = useMemo<OverviewMachineEntry[]>(() => {
    const enriched = baseEntries.map((entry) => {
      const isFault = groupMachineSets.faultIds.has(entry.canonicalId);
      const isExpiring = groupMachineSets.expiringIds.has(entry.canonicalId);
      const isUpdate = groupMachineSets.updateIds.has(entry.canonicalId);
      const faultPriority =
        (entry.lastKnownState === 'down' ? 500 : 0) +
        DATA_QUALITY_META[entry.dataQuality].priority +
        (isFault ? 50 : 0);

      return {
        ...entry,
        isFault,
        isExpiring,
        isUpdate,
        faultPriority,
      };
    });

    // 排序规则：故障异常置顶 → 到期预警次之 → 正常机器（含仅可更新机器）保持原自定义布局顺序
    return [...enriched].sort((a, b) => {
      const tierA = a.isFault ? 0 : a.isExpiring ? 1 : 2;
      const tierB = b.isFault ? 0 : b.isExpiring ? 1 : 2;
      if (tierA !== tierB) return tierA - tierB;
      if (a.isFault && b.isFault && a.faultPriority !== b.faultPriority) {
        return b.faultPriority - a.faultPriority;
      }
      return a.layoutIndex - b.layoutIndex;
    });
  }, [baseEntries, groupMachineSets]);

  const filteredEntries = useMemo(() => {
    if (!activeGroup) return machineEntries;
    return machineEntries.filter((entry) => {
      if (activeGroup === 'fault') return entry.isFault;
      if (activeGroup === 'expiring') return entry.isExpiring;
      if (activeGroup === 'update') return entry.isUpdate;
      return true;
    });
  }, [machineEntries, activeGroup]);

  const faultMachineCount = useMemo(
    () => machineEntries.filter((e) => e.isFault).length,
    [machineEntries],
  );
  const expiringMachineCount = useMemo(
    () => machineEntries.filter((e) => e.isExpiring).length,
    [machineEntries],
  );
  const updateMachineCount = useMemo(
    () => machineEntries.filter((e) => e.isUpdate).length,
    [machineEntries],
  );

  const activeObservability =
    observability &&
    (observability.active_alerts.length > 0 || observability.source_errors.length > 0)
      ? { ...observability, recent_events: [] }
      : null;

  const handleFocusItemSelect = (item: TodayFocusItem) => {
    if (item.machineId && machineEntries.some((e) => e.canonicalId === item.machineId?.toLowerCase())) {
      handleSelectGroup(item.group);
    }
  };

  const devicesHref = activeGroup ? `/devices?focus=${activeGroup}` : '/devices';

  return (
    <div
      className="w-full min-w-0 max-w-full space-y-4 overflow-x-hidden"
      data-dashboard-overview
      data-active-focus={activeGroup || 'all'}
    >
      {/* ── 首屏第一区块：B2-1 今日关注（故障置顶 → 到期 → 更新，统计分母明确） ── */}
      {hasAttention && (
        <TodayFocus
          machines={visibleMachines.length > 0 || baseEntries.length > 0 ? visibleMachines : machines}
          systems={visibleSystems.length > 0 || baseEntries.length > 0 ? visibleSystems : systems}
          observability={observability}
          services={services}
          attentionItems={resolvedAttentionItems}
          monitorUnavailable={monitorUnavailable}
          activeGroup={activeGroup}
          onSelectGroup={handleSelectGroup}
          onSelectItem={handleFocusItemSelect}
        />
      )}

      {/* ── 活动监控告警摘要（保留既有 observability 独立降级接线） ── */}
      {hasEvents && activeObservability && (
        <ObservabilitySummary data={activeObservability} machines={machines} />
      )}

      {/* ── 首屏第二区块：B2-2 设备概况卡网格（异常优先、五态新鲜度、24h 趋势、分母与更新分离） ── */}
      <section
        className="min-w-0 rounded-xl border bg-card"
        aria-label="设备简况"
        data-overview-machines
      >
        <div className="flex flex-col gap-2.5 border-b px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold">
                设备简况
                <span className="ml-1.5 font-mono text-xs font-normal tabular-nums text-muted-foreground">
                  {activeGroup
                    ? `${filteredEntries.length}/${machineEntries.length} 台`
                    : `${machineEntries.length} 台`}
                </span>
              </h2>
              {faultMachineCount > 0 ? (
                <span
                  className="inline-flex items-center gap-1 rounded-full bg-destructive/15 px-2 py-0.5 text-[11px] font-medium text-destructive"
                  data-overview-fault-summary
                >
                  <AlertTriangle className="h-3 w-3 shrink-0" />
                  <span>需处理 {faultMachineCount} 台</span>
                </span>
              ) : machineEntries.length > 0 ? (
                <span
                  className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-700 dark:text-emerald-400"
                  data-overview-healthy-summary
                >
                  <CheckCircle2 className="h-3 w-3 shrink-0" />
                  <span>运行与采样正常</span>
                </span>
              ) : null}

              {/* B1-1 §5.4：更新提示独立呈现，绝不与故障/异常混同 */}
              {updateMachineCount > 0 && (
                <span
                  className="inline-flex items-center gap-1 rounded-full border border-sky-500/25 bg-sky-500/10 px-2 py-0.5 text-[11px] text-sky-700 dark:text-sky-300"
                  title="采集端存在可用版本更新，不影响运行状态与数据质量"
                  data-overview-update-summary
                >
                  <RefreshCw className="h-3 w-3 shrink-0" />
                  <span>可更新 {updateMachineCount} 台（非故障）</span>
                </span>
              )}
            </div>

            <Link
              to={devicesHref}
              className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-md px-2 text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
            >
              <span>设备与服务</span>
              <ArrowUpRight className="h-3.5 w-3.5" />
            </Link>
          </div>

          {/* 统计分母与跨组筛选栏：确保即使隐藏「今日关注」卡片，分母契约与筛选直达依然完整 */}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div
              role="group"
              aria-label="设备简况筛选"
              className="flex flex-wrap items-center gap-1.5"
            >
              <button
                type="button"
                aria-pressed={activeGroup === null}
                data-overview-filter="all"
                onClick={() => handleSelectGroup(null)}
                className={cn(
                  'inline-flex min-h-8 items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring',
                  activeGroup === null
                    ? 'border-primary bg-primary/10 font-medium text-primary'
                    : 'border-border/60 bg-background hover:border-primary/40 text-muted-foreground',
                )}
              >
                <span>全部</span>
                <span className="font-mono text-[11px] tabular-nums">{machineEntries.length}</span>
              </button>

              {TODAY_FOCUS_GROUP_ORDER.map((groupKey) => {
                const meta = TODAY_FOCUS_GROUPS[groupKey];
                const count =
                  groupKey === 'fault'
                    ? faultMachineCount
                    : groupKey === 'expiring'
                      ? expiringMachineCount
                      : updateMachineCount;
                const selected = activeGroup === groupKey;
                return (
                  <button
                    key={groupKey}
                    type="button"
                    aria-pressed={selected}
                    data-overview-filter={groupKey}
                    onClick={() => handleSelectGroup(selected ? null : groupKey)}
                    className={cn(
                      'inline-flex min-h-8 items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring',
                      selected
                        ? 'border-primary bg-primary/10 font-medium text-primary'
                        : 'border-border/60 bg-background hover:border-primary/40 text-muted-foreground',
                    )}
                  >
                    <span>
                      {groupKey === 'fault'
                        ? '故障与异常'
                        : groupKey === 'expiring'
                          ? '即将到期'
                          : '待更新'}
                    </span>
                    <span
                      className={cn(
                        'rounded-full px-1.5 py-0.2 font-mono text-[11px] tabular-nums',
                        count > 0 ? meta.badgeClass : 'text-muted-foreground',
                      )}
                    >
                      {count}
                    </span>
                  </button>
                );
              })}
            </div>

            {/* B1-1 §8.3：明确分母「有效读数机器数 / 支持该指标机器数」 */}
            <div
              className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
              data-overview-denominator-bar
            >
              {(
                [
                  coverage.metrics.cpu,
                  coverage.metrics.memory_pct,
                  coverage.metrics.disk_pct,
                ] as const
              ).map((stat) => (
                <span
                  key={stat.metric}
                  data-overview-metric-denom={stat.metric}
                  className="inline-flex items-center gap-1 font-mono tabular-nums"
                  title={`统计分母：${stat.validCount} 台有有效读数 / ${stat.supportedCount} 台支持（不用全量台数冒充）`}
                >
                  <span className="font-sans">{stat.label}</span>
                  <span className="text-foreground">
                    {stat.average != null ? `${Math.round(stat.average)}%` : '—'}
                  </span>
                  <span>
                    ({stat.validCount}/{stat.supportedCount}台)
                  </span>
                </span>
              ))}
            </div>
          </div>
        </div>

        {/* 来源失败降级提示条（B1-2 §3.2：区分有旧快照回退 vs 无可用快照） */}
        {monitorUnavailable && (
          <div
            role="status"
            data-overview-source-failed-banner
            className="flex items-center gap-2 border-b border-destructive/20 bg-destructive/10 px-4 py-2 text-xs text-destructive"
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>
              {visibleSystems.length > 0
                ? '监控来源连接失败：当前展示最后已知快照数据，不代表实时状态'
                : '监控来源连接失败：当前暂无已缓存的采样快照'}
            </span>
          </div>
        )}

        {/* 设备概况卡片列表 / 空状态 */}
        {machineEntries.length === 0 ? (
          <div
            role="status"
            data-overview-empty="no-machines"
            className="px-4 py-8 text-center text-sm text-muted-foreground"
          >
            暂无可展示的设备概况
          </div>
        ) : filteredEntries.length === 0 ? (
          <div
            role="status"
            data-overview-empty="filtered-empty"
            className="flex flex-col items-center justify-center gap-2 px-4 py-6 text-center text-sm text-muted-foreground"
          >
            <span>
              当前「{activeGroup ? TODAY_FOCUS_GROUPS[activeGroup].title : ''}」筛选下无匹配设备
            </span>
            <button
              type="button"
              onClick={() => handleSelectGroup(null)}
              className="inline-flex min-h-9 items-center rounded-md border px-3 py-1 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
            >
              查看全部 {machineEntries.length} 台设备
            </button>
          </div>
        ) : (
          <div
            className="grid min-w-0 grid-cols-1 gap-3 p-3 sm:grid-cols-2 xl:grid-cols-3"
            data-overview-card-grid
          >
            {filteredEntries.map((entry) => {
              const cardTrend = resolveOverviewTrend(
                entry.canonicalId,
                trendsByMachine,
                entry.dataQuality,
              );
              const targetHref = entry.machine
                ? activeGroup
                  ? `/devices/${entry.machine.id}?focus=${activeGroup}`
                  : `/devices/${entry.machine.id}`
                : null;

              return (
                <div key={entry.key} className="min-w-0" data-overview-machine-item={entry.canonicalId}>
                  <MachineOverviewCard
                    machine={entry.machine ?? { id: entry.canonicalId, label: entry.label, role: 'server' }}
                    snapshot={entry.snapshot}
                    sourceFailed={monitorUnavailable && Boolean(entry.rawSystem)}
                    trend={cardTrend}
                    to={targetHref}
                  />
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* ── 第三区块：按需查看详情（保留自定义布局与隐藏偏好） ── */}
      {detailItems.length > 0 && (
        <section aria-label="按需查看详情" className="min-w-0 space-y-2">
          <h2 className="px-1 text-xs font-medium text-muted-foreground">按需查看</h2>
          {detailItems.map(({ key }) => (
            <details key={key} className="min-w-0 rounded-lg border bg-card">
              <summary className="min-h-11 cursor-pointer px-4 py-3 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">
                {resolveLabel(key)}
              </summary>
              <div className="min-w-0 px-3 pb-3">{renderWidget(key)}</div>
            </details>
          ))}
        </section>
      )}
    </div>
  );
}
