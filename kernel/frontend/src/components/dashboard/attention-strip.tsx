import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCheck, CheckCircle2, ChevronDown, ChevronRight, Undo2, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { getAttention } from '@/lib/api';
import type { AttentionItem } from '@/lib/types';
import { usePlugins } from '@/lib/plugins/provider';
import { pluginForPath } from '@/lib/plugins/registry';
import { cn } from '@/lib/utils';

const DISMISS_KEY = 'console.attention.dismissed';
const ACK_KEY = 'console.attention.acked';

export function attentionDismissKey(
  item: Pick<AttentionItem, 'kind' | 'title'> & Partial<Pick<AttentionItem, 'dismiss_key' | 'detail'>>,
): string {
  if (item.dismiss_key) return item.dismiss_key;
  // 旧服务端没有 ID 时至少纳入到期详情，避免下一期被旧标题的永久已读吞掉。
  if (item.kind === 'renewal') return `${item.kind}|${item.title}|${item.detail ?? ''}`;
  return `${item.kind}|${item.title}`;
}

function isPermanentRead(item: AttentionItem): boolean {
  return item.kind === 'renewal' && item.severity === 'error';
}

export function localDateKey(d = new Date()): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

interface DismissStore {
  date: string;
  keys: string[];
}

export function readDismissed(storage?: Storage): Set<string> {
  try {
    const raw = (storage ?? localStorage).getItem(DISMISS_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw) as DismissStore;
    if (!parsed || parsed.date !== localDateKey() || !Array.isArray(parsed.keys)) {
      return new Set();
    }
    return new Set(parsed.keys.map(String));
  } catch {
    return new Set();
  }
}

export function writeDismissed(keys: Set<string>, storage?: Storage, date?: string): void {
  try {
    const payload: DismissStore = { date: date ?? localDateKey(), keys: Array.from(keys) };
    (storage ?? localStorage).setItem(DISMISS_KEY, JSON.stringify(payload));
  } catch {
    // 隐私模式/配额异常时静默降级；调用方的内存态（dismissed Set）仍照常更新
  }
}

/**
 * 永久已读：到期提醒（kind=renewal）的过期条目点已读后跨日不再回放
 * （用户明确「到期已读后不再提示」）。key 与 DISMISS_KEY 同源，
 * 续费使用服务端 ID + 到期日 + 阶段；旧标题键无法可靠对应条目，不迁移套用。
 * 其它 error（运维告警）维持「今日忽略」语义，次日自动回来。
 */
export function readAcked(storage?: Storage): Set<string> {
  try {
    const raw = (storage ?? localStorage).getItem(ACK_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.map(String));
  } catch {
    return new Set();
  }
}

export function writeAcked(keys: Set<string>, storage?: Storage): void {
  try {
    (storage ?? localStorage).setItem(ACK_KEY, JSON.stringify(Array.from(keys)));
  } catch {
    // 与 writeDismissed 同规：存储失败不挡内存态
  }
}

interface DismissedSnapshot {
  date: string;
  keys: Set<string>;
  /** 永久已读快照；撤销时一并恢复（renewal 已读也走撤销，不永久吞掉）。 */
  acked: Set<string>;
}

interface DismissedState extends DismissedSnapshot {
  undo: DismissedSnapshot | null;
}

function currentDismissed(state: DismissedState): DismissedState {
  const date = localDateKey();
  // 同日以内存为准；重读失败的 Storage 会让本次挂载中已忽略的条目闪回。
  return state.date === date ? state : { date, keys: new Set(), acked: state.acked, undo: null };
}

interface Props {
  refreshKey?: number;
  /** 测试注入，跳过网络 */
  items?: AttentionItem[];
}

export function AttentionStrip({ refreshKey = 0, items: injected }: Props) {
  const navigate = useNavigate();
  const { states } = usePlugins();
  const [items, setItems] = useState<AttentionItem[]>(injected ?? []);
  const [loading, setLoading] = useState(injected === undefined);
  const [failed, setFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const [dismissed, setDismissed] = useState<DismissedState>(() => ({
    date: localDateKey(),
    keys: readDismissed(),
    acked: readAcked(),
    undo: null,
  }));

  useEffect(() => {
    setDismissed(currentDismissed);
    if (injected) {
      setItems(injected);
      setLoading(false);
      setFailed(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    getAttention()
      .then((data) => {
        if (!cancelled) {
          setDismissed(currentDismissed);
          setItems(data);
          setLoading(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDismissed(currentDismissed);
          setFailed(true);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, injected, retryKey]);

  const visible = useMemo(
    () => items.filter(
      (it) =>
        (!pluginForPath(it.link || '') || states.some((p) => p.id === pluginForPath(it.link || '')?.id && p.enabled && p.visible)) &&
        !dismissed.keys.has(attentionDismissKey(it)) &&
        (!isPermanentRead(it) || !dismissed.acked.has(attentionDismissKey(it))),
    ),
    [items, dismissed, states]
  );

  const dismissItems = (selected: AttentionItem[]) => {
    const today = currentDismissed(dismissed);
    const keys = new Set(today.keys);
    // 永久已读只针对到期提醒（renewal）的过期条目：用户明确「到期已读后不再提示」。
    // 其它 error（健康检查失败、容量告警等运维告警）维持「今日忽略、次日回来」，
    // 否则一次确认就永久消失违背运维直觉。撤销会一并恢复两类快照。
    const ackedAdditions = selected.filter(isPermanentRead);
    for (const item of selected) keys.add(attentionDismissKey(item));
    let nextAcked = today.acked;
    if (ackedAdditions.length > 0) {
      nextAcked = new Set(today.acked);
      for (const item of ackedAdditions) nextAcked.add(attentionDismissKey(item));
    }
    setDismissed({
      date: today.date,
      keys,
      acked: nextAcked,
      undo: { date: today.date, keys: new Set(today.keys), acked: new Set(today.acked) },
    });
    writeDismissed(keys, undefined, today.date);
    if (nextAcked !== today.acked) {
      writeAcked(nextAcked);
    }
  };

  // 只处理点击时可见条目；过期续费永久已读，其余今日忽略。
  const dismissAll = () => dismissItems(visible);

  const undoDismiss = () => {
    const today = currentDismissed(dismissed);
    const snapshot = today.undo;
    if (!snapshot || snapshot.date !== today.date) {
      // 页面未刷新时旧按钮可能仍在；先过期整份状态，不能恢复昨天的快照。
      setDismissed({ ...today, undo: null });
      return;
    }
    // 撤销时一并回滚永久已读：renewal 已读也能撤销，不会被永久吞掉。
    writeAcked(snapshot.acked);
    setDismissed({
      date: today.date,
      keys: new Set(snapshot.keys),
      acked: new Set(snapshot.acked),
      undo: null,
    });
    writeDismissed(new Set(snapshot.keys), undefined, today.date);
  };

  const count = visible.length;
  const empty = count === 0;

  return (
    <Card>
      <CardContent className="py-3">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="flex flex-1 items-center gap-2 whitespace-nowrap text-left text-sm font-medium"
            onClick={() => setCollapsed((v) => !v)}
            aria-expanded={!collapsed}
          >
            {collapsed ? (
              <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
            )}
            <span>今日关注</span>
            <span className="text-xs font-normal text-muted-foreground">
              {empty ? (failed ? '状态未知' : loading ? '加载中…' : '无待办') : `${count} 项`}
            </span>
          </button>
          {dismissed.undo && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 gap-1 px-2 text-xs text-muted-foreground"
              onClick={undoDismiss}
              aria-label="撤销上次忽略或已读"
              title="撤销本页最近一次忽略或全部已读"
            >
              <Undo2 className="h-3.5 w-3.5" />
              撤销
            </Button>
          )}
          {!empty && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 px-2 text-xs text-muted-foreground"
              onClick={dismissAll}
              aria-label="全部已读"
              title="过期续费标记已读，其余提醒今日忽略"
            >
              <CheckCheck className="h-3.5 w-3.5" />
              全部已读
            </Button>
          )}
        </div>

        {failed && (
          <div role="alert" className="mt-2 flex items-center gap-2 text-sm text-destructive">
            <span>关注信息加载失败{items.length > 0 ? '，保留上次结果' : '，当前状态未知'}</span>
            <Button type="button" variant="ghost" size="sm" onClick={() => setRetryKey((key) => key + 1)}>
              重试
            </Button>
          </div>
        )}
        {!collapsed && (
          <div className="mt-2 space-y-1">
            {empty ? (
              loading ? <p role="status" className="text-sm text-muted-foreground">正在加载关注信息…</p> :
              !failed && <p className="flex items-center gap-2 rounded-md bg-emerald-500/5 px-2 py-1.5 text-sm text-muted-foreground">
                <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
                今日无待关注，一切正常
              </p>
            ) : (
              visible.map((item) => (
                <div
                  key={attentionDismissKey(item)}
                  className="flex items-start gap-2 rounded-md px-1 py-1.5 hover:bg-muted/60"
                >
                  <button
                    type="button"
                    className="min-w-0 flex-1 text-left"
                    onClick={() => navigate(item.link || '/')}
                  >
                    <div className="flex items-center gap-2 text-sm">
                      <span
                        className={cn(
                          'shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide',
                          item.kind === 'renewal'
                            ? 'bg-amber-500/15 text-amber-700 dark:text-amber-400'
                            : item.kind === 'version'
                              ? 'bg-muted text-muted-foreground'
                              : item.severity === 'error'
                            ? 'bg-destructive/15 text-destructive'
                            : 'bg-amber-500/15 text-amber-700'
                        )}
                      >
                        {item.kind === 'renewal' ? '续费' : item.kind === 'version' ? '更新' : item.severity === 'error' ? '异常' : '留意'}
                      </span>
                      <span className="truncate font-medium">{item.title}</span>
                    </div>
                    {item.detail ? (
                      <p className="mt-0.5 truncate pl-[3.25rem] text-xs text-muted-foreground">
                        {item.detail}
                      </p>
                    ) : null}
                  </button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-7 shrink-0 px-2 text-xs text-muted-foreground"
                    onClick={() => dismissItems([item])}
                    aria-label={`忽略 ${item.title}`}
                  >
                    <X className="mr-1 h-3.5 w-3.5" />
                    {isPermanentRead(item) ? '已读' : '今日忽略'}
                  </Button>
                </div>
              ))
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
