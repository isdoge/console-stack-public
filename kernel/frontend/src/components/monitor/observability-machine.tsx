import { AlertTriangle } from 'lucide-react';
import { DisplayTime } from '@/components/ui/display-time';
import { displayTime } from '@/lib/display-time';
import {
  CollapsibleCard,
  MonitoringCoverageNotice,
  monitoringCoverageGaps,
  eventLabel,
} from '@/components/monitor/observability-shared';
import type { Machine, ObservabilityPayload, SmartDevice, SystemdService } from '@/lib/types';
import { cn } from '@/lib/utils';

/** 展示用字节格式化：GB 以上显示 GB，MB 以上显示 MB，小值显示 B。 */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return '—';
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(bytes)} B`;
}

/** 0 或 null 一律显示 —，不拿 0°C / 0 小时冒充有效读数（§4.2 已归一，这里再兜底一次）。 */
function valueOrDash(value: number | null | undefined, unit: string): string {
  return value ? `${value}${unit}` : '—';
}

function systemdFailed(u: SystemdService): boolean {
  return u.state === 'failed' || u.sub_state === 'failed';
}

/** SMART state 非空且不（大小写无关）等于 PASSED → 异常。 */
function smartBad(disk: SmartDevice): boolean {
  return !!disk.state && disk.state.toUpperCase() !== 'PASSED';
}

function SystemdCard({ units }: { units: SystemdService[] }) {
  return (
    <CollapsibleCard
      storageKey={`machine-systemd-${units[0]?.machine || 'x'}`}
      title="systemd 服务"
      contentClassName="space-y-1.5"
    >
      {units.map((u) => {
        const bad = systemdFailed(u);
        return (
          <div
            key={u.unit}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2 py-1.5"
          >
            <span className="min-w-0 break-all font-mono text-sm" title={u.unit}>
              {u.unit}
            </span>
            <span
              className={cn(
                'rounded px-1.5 py-0.5 text-[11px]',
                bad
                  ? 'bg-destructive/15 text-destructive'
                  : 'bg-muted text-muted-foreground'
              )}
            >
              {u.state} · {u.sub_state}
            </span>
            <span className="ml-auto flex shrink-0 gap-3 font-mono text-xs text-muted-foreground">
              <span title="CPU 使用率">CPU {u.cpu_pct != null ? `${u.cpu_pct.toFixed(1)}%` : '—'}</span>
              <span title="内存占用">{formatBytes(u.memory_bytes)}</span>
            </span>
          </div>
        );
      })}
    </CollapsibleCard>
  );
}

function StorageCard({ disks }: { disks: SmartDevice[] }) {
  return (
    <CollapsibleCard
      storageKey={`machine-storage-${disks[0]?.machine || 'x'}`}
      title="存储设备"
      contentClassName="grid gap-2 sm:grid-cols-2"
    >
      {disks.map((d) => {
        const bad = smartBad(d);
        return (
          <div key={d.device} className="rounded-md bg-muted/40 px-2.5 py-2">
            <div className="flex items-center justify-between gap-2">
              <span className="min-w-0 truncate font-mono text-sm" title={d.device}>
                {d.device}
              </span>
              <span
                className={cn(
                  'shrink-0 rounded px-1.5 py-0.5 text-[11px]',
                  bad
                    ? 'bg-destructive/15 text-destructive'
                    : 'bg-emerald-500/15 text-emerald-600'
                )}
              >
                {d.state || '—'}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
              {d.type && <span>{d.type}</span>}
              {d.model && <span className="truncate" title={d.model}>{d.model}</span>}
              <span>{formatBytes(d.capacity_bytes)}</span>
              <span>温度 {valueOrDash(d.temperature_c, '°C')}</span>
              <span>通电 {valueOrDash(d.power_on_hours, ' 小时')}</span>
              <span>循环 {valueOrDash(d.cycles, '')}</span>
            </div>
          </div>
        );
      })}
    </CollapsibleCard>
  );
}

interface Props {
  machine: Machine;
  /** 未拉取或失败为 null → 不渲染 */
  data?: ObservabilityPayload | null;
  /** T7：块级显隐（设备详情「显示模块」菜单）；缺省全显示 */
  visible?: { events?: boolean; systemd?: boolean; storage?: boolean };
}

/**
 * J-D D3：单机页 Beszel 扩展可见性（§4.2）。
 * - 本机事件最多 10 条，事件名经 eventLabel 中文化（title 保留原始规则名）；卡片可折叠。
 * - systemd 区只在本机有白名单记录时显示；存储区只在本机有 SMART/mdraid 设备时显示。
 * - source_errors 是 Beszel 全局 collection 状态，单机页必须明确标注“全局”。
 * - 0/null 温度/小时显示 —；failed / 非 PASSED 用 destructive。
 * - **本组件是纯展示**：KR/SG 的操作按钮只来自 MachineCard 的 `machine.actions`，
 *   US 的 actions 为空 → 永不出现 restart；这里不新增任何动作逻辑。
 */
export function ObservabilityMachine({ machine, data, visible }: Props) {
  if (!data) return null;
  const events = data.recent_events.filter((e) => e.machine === machine.id).slice(0, 10);
  const units = data.systemd_services.filter((s) => s.machine === machine.id);
  const disks = data.smart_devices.filter((d) => d.machine === machine.id);
  const errors = data.source_errors;
  const coverageGaps = monitoringCoverageGaps(data, visible);
  const showEvents = visible?.events !== false && events.length > 0;
  const showSystemd = visible?.systemd !== false && units.length > 0;
  const showStorage = visible?.storage !== false && disks.length > 0;
  if (!showEvents && !showSystemd && !showStorage && errors.length === 0 && coverageGaps.length === 0) {
    return null;
  }

  return (
    <div className="space-y-6">
      <MonitoringCoverageNotice gaps={coverageGaps} />
      {errors.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div className="min-w-0">
            <div className="font-medium">Console 全局监控数据部分不可用</div>
            <div
              className="mt-0.5 truncate text-amber-700/80"
              title={errors.map((e) => `${e.source}: ${e.message}`).join('；')}
            >
              {errors.map((e) => `${e.source}: ${e.message}`).join('；')}
            </div>
          </div>
        </div>
      )}

      {showEvents && (
        <CollapsibleCard
          storageKey={`machine-events-${machine.id}`}
          title="本机事件"
          summary={events.some((e) => e.state !== 'resolved') ? '有进行中' : undefined}
          contentClassName="space-y-1"
        >
          {events.map((e, i) => {
            const resolved = e.state === 'resolved' || e.resolved_at != null;
            return (
              <div
                key={`${e.name}-${i}`}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2 py-1.5"
              >
                <span
                  className="min-w-0 flex-1 truncate text-sm"
                  title={`${e.name} · ${displayTime(e.opened_at).title}`}
                >
                  <span className="font-medium">{eventLabel(e.name)}</span>
                  <span className="text-muted-foreground"> · <DisplayTime value={e.opened_at} /></span>
                </span>
                <span
                  className={cn(
                    'shrink-0 text-xs',
                    resolved ? 'text-muted-foreground' : 'text-destructive'
                  )}
                >
                  {resolved ? '已恢复' : '进行中'}
                </span>
              </div>
            );
          })}
        </CollapsibleCard>
      )}

      {showSystemd && <SystemdCard units={units} />}
      {showStorage && <StorageCard disks={disks} />}
    </div>
  );
}
