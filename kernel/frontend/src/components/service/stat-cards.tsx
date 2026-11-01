import { Activity, CheckCircle2, RefreshCw, Server } from 'lucide-react';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

export type StatFilter = 'running' | 'healthy' | 'outdated' | null;

interface StatCardsProps {
  /* 现有 props 全部保留、语义不变（§4.4 向后兼容） */
  total: number;
  running: number;
  healthy: number;
  outdated: number;
  loading?: boolean;
  active: StatFilter;
  onSelect: (filter: StatFilter) => void;
  /* §4.4：D5 新增。undefined 不渲染离线 chip；>0 才显示，避免「离线 0」噪声 */
  offlineServers?: number;
  onOfflineClick?: () => void;
}

const CARDS = [
  { key: 'total', label: '服务总数', icon: Server, color: 'text-blue-500', filter: null },
  { key: 'running', label: '运行中', icon: Activity, color: 'text-emerald-500', filter: 'running' },
  { key: 'healthy', label: '健康', icon: CheckCircle2, color: 'text-violet-500', filter: 'healthy' },
  { key: 'outdated', label: '待更新', icon: RefreshCw, color: 'text-amber-500', filter: 'outdated' },
] as const;

const OFFLINE_CHIP_CLASS =
  'inline-flex h-[30px] shrink-0 items-center gap-2 whitespace-nowrap rounded-full border border-destructive/40 bg-destructive/5 px-3 text-sm text-destructive sm:h-auto sm:py-1.5';

function OfflineChip({ count, onClick }: { count: number; onClick?: () => void }) {
  const body = (
    <>
      <span>服务器离线</span>
      <span className="rounded-full bg-destructive/15 px-2 py-0.5 font-mono text-xs font-semibold tabular-nums">
        {count}
      </span>
    </>
  );
  if (!onClick) {
    return (
      <span className={OFFLINE_CHIP_CLASS} title="监控里状态非 up 的服务器数">
        {body}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={cn(OFFLINE_CHIP_CLASS, 'transition-colors hover:bg-destructive/10')}
      onClick={onClick}
      title="查看离线服务器"
    >
      {body}
    </button>
  );
}

export function StatCards({
  total,
  running,
  healthy,
  outdated,
  loading,
  active,
  onSelect,
  offlineServers,
  onOfflineClick,
}: StatCardsProps) {
  const values = { total, running, healthy, outdated };
  return (
    <div role="group" aria-label="选择服务状态" className="scrollbar-hidden flex w-full min-w-0 items-center gap-2 overflow-x-auto overscroll-x-contain sm:flex-wrap">
      {CARDS.map((card) => {
        const Icon = card.icon;
        const isActive = card.filter != null && active === card.filter;
        return (
          <button
            key={card.key}
            type="button"
            aria-pressed={isActive}
            title={card.filter ? `点击筛选${card.label}的服务，再点取消` : '点击清除筛选'}
            className={cn(
              'inline-flex h-[30px] shrink-0 items-center gap-2 whitespace-nowrap rounded-full border border-border/60 bg-card/75 px-3 text-sm transition-colors hover:border-primary/50 sm:h-auto sm:py-1.5',
              isActive && 'border-primary bg-primary/10 text-primary'
            )}
            onClick={() => onSelect(isActive ? null : card.filter)}
          >
            <Icon className={cn('h-3.5 w-3.5', card.color)} />
            <span className="text-muted-foreground">{card.label}</span>
            {/* 骨架用 muted-foreground/20：徽章底色已是 bg-muted，同色会让 loading 态看不见 */}
            <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-xs font-semibold tabular-nums">
              {loading ? (
                <Skeleton className="h-3 w-6 bg-muted-foreground/20" />
              ) : (
                values[card.key]
              )}
            </span>
          </button>
        );
      })}
      {offlineServers != null && offlineServers > 0 && (
        // 无 onOfflineClick（设备页本身就在服务器区旁边）时退化为纯提示：
        // 渲染成 span 而不是点了没反应的 button
        <OfflineChip count={offlineServers} onClick={onOfflineClick} />
      )}
    </div>
  );
}
