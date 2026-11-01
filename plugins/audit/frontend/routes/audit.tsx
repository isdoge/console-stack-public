import { useSearchParams } from 'react-router-dom';
import { DisplayTime } from '@/components/ui/display-time';
import { useEffect, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { EventTimeline } from '../components/monitor/event-timeline';
import { listOplogs } from '@/lib/api';
import type { OpLog } from '@/lib/types';

interface Props {
  refreshKey: number;
}

const LEDGER_ACTION_LABELS: Record<string, string> = {
  ledger_manual: '手工记账', ledger_category: '修改记账分类', ledger_note: '修改记账备注',
  ledger_offset: '记账关联与对冲', ledger_bulk_category: '批量记账分类',
  ledger_dict_add: '新增记账分类', ledger_dict_rename: '重命名记账分类',
  ledger_dict_delete: '删除记账分类', ledger_import: '导入账单',
  ledger_reclassify: '重跑未分类', ledger_relink: '重建账单关联', ledger_link_repay: '关联信用卡还款',
};

/**
 * 审计页 = 两条历史流水，共用一个页面（Track K4）：
 * - 「监控事件」：机器/服务的状态转移（谁坏了、坏了多久、恢复没有）；
 * - 「操作记录」：oplog，控制台自己执行过的动作。
 *
 * 两者刻意**不合表**：oplog 由控制台动作写，事件由探测器写，
 * 合并只在这一层做展示（设计 §5.4「事件页可以合并展示，但不复制写入两份表」）。
 */
export function AuditRoute({ refreshKey }: Props) {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') === 'oplog' ? 'oplog' : 'events';
  return (
    <Tabs value={tab} onValueChange={(value) => setParams((prev) => { const next = new URLSearchParams(prev); next.set('tab', value); return next; }, { replace: true })}>
      <TabsList>
        <TabsTrigger value="events">监控事件</TabsTrigger>
        <TabsTrigger value="oplog">操作记录</TabsTrigger>
      </TabsList>
      <TabsContent value="events">
        <Card>
          <CardHeader className="pb-0">
            <CardTitle className="text-base">监控事件</CardTitle>
            <p className="text-xs text-muted-foreground">
              只记状态变化：一行 = 一次故障（含持续时长），不是采样点。未恢复的事件不受时间窗裁剪。
            </p>
          </CardHeader>
          <CardContent className="px-0 pb-0 pt-3">
            <EventTimeline refreshKey={refreshKey} />
          </CardContent>
        </Card>
      </TabsContent>
      <TabsContent value="oplog">
        <OplogTable key={`${params.get('q')}|${params.get('host')}|${params.get('svc')}`} refreshKey={refreshKey}
          initialFilter={params.get('q') || ''} host={params.get('host') || ''} svc={params.get('svc') || ''} />
      </TabsContent>
    </Tabs>
  );
}

function OplogTable({ refreshKey, initialFilter, host, svc }: Props & { initialFilter: string; host: string; svc: string }) {
  const [rows, setRows] = useState<OpLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState(initialFilter);
  const [scoped, setScoped] = useState(Boolean(host && svc));
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    // 新筛选条件不能沿用上一条件的记录；同条件刷新失败保留结果。
    // 关键字筛选走后端 LIKE（host/svc/action），300ms 防抖避免逐键请求
    const timer = setTimeout(() => {
      listOplogs(filter.trim() || undefined)
        .then((data) => {
          if (!cancelled) setRows(scoped ? data.filter((row) => row.host === host && row.svc === svc) : data);
        })
        .catch(() => {
          if (!cancelled) setError('获取审计日志失败');
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, filter ? 300 : 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [refreshKey, filter, retry, host, svc, scoped]);

  const changeFilter = (value: string) => {
    setFilter(value);
    setScoped(false);
    setRows([]);
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">操作记录</CardTitle>
          {scoped && <span className="text-xs text-muted-foreground">{host} / {svc} · 最近 100 条搜索结果内精确匹配</span>}
          <div className="flex items-center gap-2">
            <Input
              value={filter}
              onChange={(e) => changeFilter(e.target.value)}
              placeholder="筛选：主机 / 服务 / 动作"
              aria-label="筛选审计日志"
              className="h-8 w-56 text-sm"
            />
            {filter && (
              <Button variant="ghost" size="sm" onClick={() => changeFilter('')}>
                清除
              </Button>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <p className="px-4 pb-2 text-xs text-muted-foreground">带时区记录显示为本地时间；旧记录未标时区，保留服务器原始时间。</p>
        {error && (
          <div role="alert" className="flex items-center justify-center gap-2 p-4 text-sm text-destructive">
            {error}
            <Button variant="outline" size="sm" disabled={loading} onClick={() => setRetry((value) => value + 1)}>
              重试
            </Button>
          </div>
        )}
        {loading && rows.length === 0 ? (
          <div className="p-6 space-y-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          !error && <p className="p-6 text-center text-sm text-muted-foreground">暂无操作记录</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/30 text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left">ID</th>
                  <th className="px-4 py-2 text-left">时间</th>
                  <th className="px-4 py-2 text-left">主机</th>
                  <th className="px-4 py-2 text-left">服务</th>
                  <th className="px-4 py-2 text-left">动作</th>
                  <th className="px-4 py-2 text-left">结果</th>
                  <th className="px-4 py-2 text-left">操作者</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((log) => {
                  const ok = String(log.result || '').startsWith('Success');
                  const action = Object.prototype.hasOwnProperty.call(LEDGER_ACTION_LABELS, log.action)
                    ? LEDGER_ACTION_LABELS[log.action] : log.action;
                  return (
                    <tr key={log.id} className="border-b last:border-0 hover:bg-muted/30">
                      <td className="px-4 py-2 font-mono text-xs text-muted-foreground">{log.id}</td>
                      <td className="px-4 py-2 text-xs"><DisplayTime value={log.started_at} /></td>
                      <td className="px-4 py-2 text-xs">{log.host || '-'}</td>
                      <td className="px-4 py-2 font-medium">{log.svc || '-'}</td>
                      <td className="px-4 py-2">
                        <Badge variant="outline" title={log.action || undefined}>{action || '-'}</Badge>
                      </td>
                      <td className="px-4 py-2">
                        <Badge variant={ok ? 'success' : 'destructive'} className="max-w-[280px] truncate">
                          {log.result || '-'}
                        </Badge>
                      </td>
                      <td className="px-4 py-2 text-xs text-muted-foreground">{log.operator || 'self'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
