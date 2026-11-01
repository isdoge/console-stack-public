import { DisplayTime } from '@/components/ui/display-time';
import { displayTime } from '@/lib/display-time';
import { AlertTriangle } from 'lucide-react';
import type { Machine, ObservabilityPayload } from '@/lib/types';
import { cn } from '@/lib/utils';
import {
  CollapsibleCard,
  MonitoringCoverageNotice,
  monitoringCoverageGaps,
  SeverityChip,
  eventLabel,
} from '@/components/monitor/observability-shared';

/** 浏览器本地时间，原始时区由 DisplayTime 的 title 保留。 */
export function formatObservabilityTime(iso?: string | null): string {
  return displayTime(iso).text;
}

/** 台账 id → 显示名；取不到就大写（Beszel 系统名风格）。 */
function machineLabel(id: string, machines?: Machine[]): string {
  const m = machines?.find((x) => x.id === id);
  return m?.label || id.toUpperCase();
}



interface Props {
  /** 未拉取或拉取失败时为 null → 不渲染，绝不占一张空卡 */
  data?: ObservabilityPayload | null;
  /** 台账列表，用于把机器 id 显示成可读名 */
  machines?: Machine[];
}

/**
 * J-D D2：总控台告警/事件摘要（§4.2）。
 * - 卡片可折叠（状态持久 localStorage），折叠时标题旁保留「告警 N · 事件 N」摘要。
 * - 事件名经 eventLabel 中文化（Status→在线状态等），title 保留原始规则名。
 * - `source_errors` 非空时提示「部分监控数据暂不可用」，不影响其它数组展示。
 * - 空数组时不占一张大空卡（直接返回 null）。
 * - Beszel 深链用后端合成的 `machine_links`，前端不拼 PocketBase URL。
 */
export function ObservabilitySummary({ data, machines }: Props) {
  if (!data) return null;
  const alerts = data.active_alerts.slice(0, 5);
  const events = data.recent_events.slice(0, 5);
  const errors = data.source_errors;
  const coverageGaps = monitoringCoverageGaps(data);
  if (alerts.length === 0 && events.length === 0 && errors.length === 0 && coverageGaps.length === 0) return null;

  const summary = [
    data.active_alerts.length > 0 || data.recent_events.length > 0
      ? `告警 ${data.active_alerts.length} · 事件 ${data.recent_events.length}`
      : '',
    coverageGaps.length > 0 ? `${coverageGaps.length} 项未覆盖` : '',
  ].filter(Boolean).join(' · ') || undefined;

  return (
    <CollapsibleCard
      storageKey="observability-summary"
      title="监控事件"
      summary={summary}
      extras={
        data.updated_at ? (
          <span className="text-xs text-muted-foreground">
            <DisplayTime value={data.updated_at} />
          </span>
        ) : undefined
      }
      contentClassName="space-y-3"
    >
      <MonitoringCoverageNotice gaps={coverageGaps} />
      {errors.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div className="min-w-0">
            <div className="font-medium">部分监控数据暂不可用</div>
            <div className="mt-0.5 truncate text-amber-700/80" title={errors.map((e) => `${e.source}: ${e.message}`).join('；')}>
              {errors.map((e) => `${e.source}: ${e.message}`).join('；')}
            </div>
          </div>
        </div>
      )}

      {alerts.length > 0 && (
        <section className="space-y-1">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            活动告警
          </h3>
          {alerts.map((a) => (
            <div
              key={`${a.machine}-${a.name}`}
              className="flex items-center gap-2 rounded-md px-2 py-1.5"
            >
              <SeverityChip severity={a.severity} />
              <div
                className="min-w-0 flex-1 truncate text-sm"
                title={`${machineLabel(a.machine, machines)} · ${a.name}`}
              >
                <span className="font-medium">{machineLabel(a.machine, machines)}</span>
                <span className="text-muted-foreground"> · {eventLabel(a.name)}</span>
              </div>
              <span className="shrink-0 text-xs text-destructive">触发中</span>
            </div>
          ))}
        </section>
      )}

      {events.length > 0 && (
        <section className="space-y-1">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            最近事件
          </h3>
          {events.map((e, i) => {
            const resolved = e.state === 'resolved' || e.resolved_at != null;
            return (
              <div
                key={`${e.machine}-${e.name}-${i}`}
                className="flex items-center gap-2 rounded-md px-2 py-1.5"
              >
                <SeverityChip severity={e.severity} />
                <div className="min-w-0 flex-1">
                  <div
                    className="truncate text-sm"
                    title={`${machineLabel(e.machine, machines)} · ${e.name}`}
                  >
                    <span className="font-medium">{machineLabel(e.machine, machines)}</span>
                    <span className="text-muted-foreground"> · {eventLabel(e.name)}</span>
                  </div>
                  <div className="truncate text-xs text-muted-foreground">
                    <DisplayTime value={e.opened_at} />
                    {resolved && e.duration_sec != null ? ` · ${Math.round(e.duration_sec)}s` : ''}
                  </div>
                </div>
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
        </section>
      )}
    </CollapsibleCard>
  );
}
