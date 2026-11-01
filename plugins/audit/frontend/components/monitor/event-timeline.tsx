import { DisplayTime } from '@/components/ui/display-time';
import { displayTime } from '@/lib/display-time';
import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, CheckCircle2, CornerDownRight, Globe, Server, Wrench } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { listMonitorEvents } from '@/lib/api';
import type { MonitorEvent, MonitorEventKind, MonitorEventPage } from '@/lib/types';
import { cn } from '@/lib/utils';

const PAGE_SIZE = 50;

type KindFilter = MonitorEventKind | 'all';
type StateFilter = 'active' | 'resolved' | 'all';

const KIND_OPTIONS: { value: KindFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'machine', label: '机器' },
  { value: 'service', label: '服务' },
  // 家宽公网不通：机器在线、代理端口从家宽连不上（与机器事件分开，不折叠）
  { value: 'public', label: '公网' },
];

const STATE_OPTIONS: { value: StateFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '未恢复' },
  { value: 'resolved', label: '已恢复' },
];

const DAY_OPTIONS = [7, 30, 90];

/** 浏览器本地时间，原始时区由 DisplayTime 的 title 保留。 */
export function formatEventTime(iso?: string | null): string {
  return displayTime(iso).text;
}

/** 持续时长的人话版本。活动事件给的是「截至此刻」，会随刷新变大。 */
export function formatEventDuration(sec?: number | null): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return '—';
  if (sec < 60) return `${Math.floor(sec)} 秒`;
  const minutes = Math.floor(sec / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时 ${minutes % 60} 分`;
  const days = Math.floor(hours / 24);
  return `${days} 天 ${hours % 24} 小时`;
}

function FilterRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </div>
  );
}

function EventRow({ event }: { event: MonitorEvent }) {
  const active = event.state === 'active';
  // 有根因覆盖的服务事件缩进一格：它是主机故障的影响面，不是独立故障。
  // 根可能落在别的分页上，所以只做视觉标记，不重排列表（后端刻意返回扁平列表）。
  const nested = Boolean(event.collapsed_by);
  // 保持后端 kind/fingerprint 不变，行内区分规则告警、采集中断与普通探测。
  const category = event.kind !== 'machine' ? null
    : event.fingerprint.startsWith('machine:alert:') ? '规则告警'
      : event.fingerprint.startsWith('machine:monitoring:') ? '采集中断' : null;
  const Icon = event.kind === 'machine' ? Server : event.kind === 'public' ? Globe : Wrench;

  return (
    <div
      className={cn(
        'flex items-start gap-3 border-b px-4 py-3 last:border-0 hover:bg-muted/30',
        nested && 'pl-10'
      )}
      data-testid="event-row"
    >
      {nested ? (
        <CornerDownRight
          className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden
        />
      ) : (
        <Icon
          className={cn(
            'mt-0.5 h-4 w-4 shrink-0',
            active ? 'text-destructive' : 'text-muted-foreground'
          )}
          aria-hidden
        />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm font-medium">{event.title}</span>
          {category && <Badge variant="outline" className="shrink-0">{category}</Badge>}
          {active ? (
            <Badge variant="destructive" className="shrink-0">
              未恢复
            </Badge>
          ) : (
            <Badge variant="success" className="shrink-0">
              已恢复
            </Badge>
          )}
          {active && (event.impact_count ?? 0) > 0 ? (
            <Badge variant="outline" className="shrink-0">
              影响 {event.impact_count} 项
            </Badge>
          ) : null}
        </div>
        {event.detail ? (
          <p className="mt-0.5 break-words text-xs text-muted-foreground">{event.detail}</p>
        ) : null}
        <p className="mt-1 text-xs text-muted-foreground">
          <DisplayTime value={event.opened_at} />
          {' · 持续 '}
          {formatEventDuration(event.duration_sec)}
          {event.resolved_at && <> · 恢复于 <DisplayTime value={event.resolved_at} /></>}
          {event.machine ? ` · ${event.machine}` : ''}
        </p>
      </div>
    </div>
  );
}

interface Props {
  refreshKey?: number;
  /** 测试注入，跳过网络 */
  page?: MonitorEventPage;
}

/**
 * Track K4 事件时间线：机器/服务的状态转移，只读。
 *
 * - 后端只在状态**变化**时写行，所以这里一行 = 一次完整故障（含持续时长），
 *   不是采样点；密度远低于设备页的探测色带。
 * - 活动事件排在已恢复之前，且**不受时间窗裁剪**（开了 30 天的故障在「7 天」
 *   视图里仍然看得见），窗口只管已恢复的历史。
 * - 折叠只做视觉标记（根因带「影响 N 项」、影响面缩进），不重排列表 —— 一个根
 *   的影响面可能跨分页。
 */
export function EventTimeline({ refreshKey = 0, page: injected }: Props) {
  const [page, setPage] = useState<MonitorEventPage | null>(injected ?? null);
  const [loading, setLoading] = useState(!injected);
  const [failed, setFailed] = useState(false);
  const [kind, setKind] = useState<KindFilter>('all');
  const [state, setState] = useState<StateFilter>('all');
  const [days, setDays] = useState(7);
  const [machine, setMachine] = useState('');
  const [offset, setOffset] = useState(0);
  const [retry, setRetry] = useState(0);
  // 机器筛选项从已见事件里累积，不额外请求 /api/machines。刻意「只增不减」：
  // 若每次都按当前结果集重算，筛到某台机器后其它 chip 会消失，等于把自己锁死。
  const [knownMachines, setKnownMachines] = useState<string[]>([]);

  useEffect(() => {
    if (injected) {
      setPage(injected);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    listMonitorEvents({
      days,
      kind: kind === 'all' ? undefined : kind,
      machine: machine || undefined,
      state,
      limit: PAGE_SIZE,
      offset,
    })
      .then((data) => {
        if (cancelled) return;
        const lastOffset = Math.max(0, Math.floor((data.total - 1) / PAGE_SIZE) * PAGE_SIZE);
        if (offset > lastOffset) {
          setOffset(lastOffset);
          setPage(null);
          return;
        }
        setPage(data);
        setFailed(false);
        setKnownMachines((prev) => {
          const merged = new Set(prev);
          for (const item of data.events) {
            if (item.machine) merged.add(item.machine);
          }
          return merged.size === prev.length ? prev : Array.from(merged).sort();
        });
      })
      .catch(() => {
        if (cancelled) return;
        setPage(null);
        setFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, injected, days, kind, machine, state, offset, retry]);

  const events = page?.events ?? [];
  const total = page?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  const summary = useMemo(() => {
    if (failed) return '加载失败';
    if (!page) return '';
    return page.active_count > 0
      ? `${page.active_count} 项未恢复 · 共 ${total} 条`
      : `共 ${total} 条`;
  }, [failed, page, total]);

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3">
        <FilterRow label="类型">
          {KIND_OPTIONS.map((option) => (
            <Button
              key={option.value}
              variant={kind === option.value ? 'secondary' : 'ghost'}
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => { setKind(option.value); setOffset(0); }}
              aria-pressed={kind === option.value}
            >
              {option.label}
            </Button>
          ))}
        </FilterRow>
        <FilterRow label="状态">
          {STATE_OPTIONS.map((option) => (
            <Button
              key={option.value}
              variant={state === option.value ? 'secondary' : 'ghost'}
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => { setState(option.value); setOffset(0); }}
              aria-pressed={state === option.value}
            >
              {option.label}
            </Button>
          ))}
        </FilterRow>
        <FilterRow label="窗口">
          {DAY_OPTIONS.map((option) => (
            <Button
              key={option}
              variant={days === option ? 'secondary' : 'ghost'}
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => { setDays(option); setOffset(0); }}
              aria-pressed={days === option}
            >
              {option} 天
            </Button>
          ))}
        </FilterRow>
        {/* 只有一台机器出过事时不必给筛选 —— 选项等于没得选 */}
        {knownMachines.length > 1 ? (
          <FilterRow label="机器">
            <Button
              variant={machine === '' ? 'secondary' : 'ghost'}
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => { setMachine(''); setOffset(0); }}
              aria-pressed={machine === ''}
            >
              全部
            </Button>
            {knownMachines.map((id) => (
              <Button
                key={id}
                variant={machine === id ? 'secondary' : 'ghost'}
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => { setMachine(id); setOffset(0); }}
                aria-pressed={machine === id}
              >
                {id}
              </Button>
            ))}
          </FilterRow>
        ) : null}
        <span className="ml-auto text-xs text-muted-foreground">{summary}</span>
      </div>

      {loading ? (
        <div className="space-y-2 p-6">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-10 w-full" />
          ))}
        </div>
      ) : failed ? (
        <div role="alert" className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
          <AlertCircle className="h-4 w-4 shrink-0" />
          事件列表加载失败
          <Button variant="outline" size="sm" onClick={() => setRetry((value) => value + 1)}>
            重试
          </Button>
        </div>
      ) : events.length === 0 ? (
        <p className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
          该窗口内没有状态变化
        </p>
      ) : (
        <div>
          {events.map((event) => (
            <EventRow key={event.id} event={event} />
          ))}
        </div>
      )}

      {(hasPrev || hasNext) && !loading && !failed ? (
        <div className="flex items-center justify-end gap-2 px-4 py-3">
          <span className="mr-auto text-xs text-muted-foreground">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} / {total}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={!hasPrev}
            onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={!hasNext}
            onClick={() => setOffset((value) => value + PAGE_SIZE)}
          >
            下一页
          </Button>
        </div>
      ) : null}
    </div>
  );
}
