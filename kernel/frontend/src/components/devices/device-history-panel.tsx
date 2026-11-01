import { useEffect, useState } from 'react';
import { DeviceHistoryChart } from './device-history-chart';
import type { MachineOverviewSnapshot } from '@/components/dashboard/machine-overview-card';
import { Button } from '@/components/ui/button';
import { getMonitorHistory } from '@/lib/api';
import type { Machine, MonitorHistoryPayload } from '@/lib/types';

const RANGES = [
  { label: '1 小时', hours: 1 },
  { label: '24 小时', hours: 24 },
  { label: '7 天', hours: 168 },
  { label: '30 天', hours: 720 },
];
const METRICS = ['cpu', 'mem_pct', 'disk_pct', 'load1'];

export function DeviceHistoryPanel({ machine, snapshot, source, refreshKey }: {
  machine: Machine;
  snapshot?: MachineOverviewSnapshot | null;
  source?: string;
  refreshKey: number;
}) {
  const [hours, setHours] = useState(24);
  const [reload, setReload] = useState(0);
  const [result, setResult] = useState<{ machine: string; hours: number; data: MonitorHistoryPayload } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const current = result?.machine === machine.id && result.hours === hours ? result.data : null;

  useEffect(() => {
    if (source !== 'console_pg') return;
    let alive = true;
    const end = new Date();
    setLoading(true);
    setError('');
    getMonitorHistory({ machine: machine.id, metrics: METRICS,
      start: new Date(end.getTime() - hours * 3600_000).toISOString(), end: end.toISOString() })
      .then((data) => {
        if (alive) setResult({ machine: machine.id, hours, data });
      })
      .catch(() => {
        if (alive) setError('历史数据暂时无法读取');
      })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [machine.id, source, hours, refreshKey, reload]);

  return (
    <section aria-label="历史与事件" className="min-w-0 space-y-2" data-device-history-panel>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <div role="group" aria-label="历史时间范围" className="flex flex-wrap gap-1">
          {RANGES.map((range) => (
            <Button key={range.hours} size="sm" variant={hours === range.hours ? 'secondary' : 'ghost'}
              className="h-8 px-2 text-xs" aria-pressed={hours === range.hours} onClick={() => setHours(range.hours)}>
              {range.label}
            </Button>
          ))}
        </div>
        <span className="text-xs text-muted-foreground">{source === 'console_pg' ? 'Console PG · 主机指标与同窗事件' : '当前来源尚未接入历史曲线'}</span>
      </div>
      {error && <div role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
        <span>{error}{current ? '，保留上次结果。' : '，请稍后重试。'}</span>
        <Button variant="ghost" size="sm" onClick={() => setReload((value) => value + 1)}>重试历史查询</Button>
      </div>}
      <div aria-busy={loading} className={loading && current ? 'opacity-60' : undefined}>
        <DeviceHistoryChart machine={machine} snapshot={snapshot} history={source === 'console_pg' ? current : null}
          events={source === 'console_pg' ? current?.events : undefined} loading={loading && !current} />
      </div>
    </section>
  );
}
