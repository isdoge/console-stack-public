import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { formatGb, formatMbPair, priceText, uptimeText } from '@/components/monitor/machine-card';
import { cn } from '@/lib/utils';
import type { CapacityPayload, Machine, MonitorSystem } from '@/lib/types';

/**
 * M3 上限（设计 §7 Track M）。四列之外表格会横向溢出，而且「同时盯四台以上」
 * 本身就不是对比，是看总览——总览已经有 /devices 的卡片墙了。
 */
export const COMPARE_LIMIT = 4;

function pctText(value?: number | null) {
  return value == null ? '—' : `${value.toFixed(1)}%`;
}

/** 一行对比数据：`values[i]` 对应 `machines[i]`；`tone` 逐列上色（缺省不上色）。 */
export interface CompareRow {
  label: string;
  values: string[];
  tones?: Array<string | undefined>;
}

/**
 * 把选中的机器摊成对比行。**纯函数、无副作用**，单测直接喂数据断言，
 * 不必渲染整个对话框。
 *
 * 取数一律来自调用方已经加载好的 machines / monitor / capacity 三份数据，
 * 本组件**不发任何请求**——对比是个临时视图，不该因为打开它就多打一轮后端。
 *
 * 缺数据统一显示 `—`：node 没有 Beszel 指标、server 没有流量配额、样本不足
 * 没有磁盘趋势，这些都是正常状态，不是错误，不需要额外标注。
 */
export function buildCompareRows(
  machines: Machine[],
  systemByMachine: Record<string, MonitorSystem>,
  capacity: CapacityPayload | null
): CompareRow[] {
  const sys = machines.map((m) => systemByMachine[m.id]);
  const trends = machines.map((m) => capacity?.machines?.[m.id]);

  return [
    {
      label: '角色',
      values: machines.map((m) => (m.role === 'node' ? '节点' : m.role === 'server' ? '服务器' : m.role)),
    },
    {
      label: '状态',
      // 与卡片同口径（§4.3）：node 看探测结果，server 看 Beszel status
      values: machines.map((m, i) => (m.role === 'node' ? m.probe_status : sys[i]?.status) || 'unknown'),
    },
    { label: '系统', values: machines.map((m) => m.os || '—') },
    { label: '在线', values: machines.map((_m, i) => uptimeText(sys[i]?.uptime_sec) || '—') },
    { label: 'CPU', values: machines.map((_m, i) => pctText(sys[i]?.cpu)) },
    {
      label: '内存',
      values: machines.map((_m, i) =>
        formatMbPair(sys[i]?.memory_used_mb, sys[i]?.memory_total_mb) || pctText(sys[i]?.memory_pct)
      ),
    },
    {
      label: '硬盘',
      values: machines.map((_m, i) =>
        sys[i]?.disk_used_gb != null && sys[i]?.disk_total_gb != null
          ? `${formatGb(sys[i]!.disk_used_gb)} / ${formatGb(sys[i]!.disk_total_gb)}`
          : '—'
      ),
    },
    {
      label: '本月出站',
      values: machines.map((m) =>
        m.traffic
          ? m.traffic.quota_gb
            ? `${formatGb(m.traffic.used_gb)} / ${formatGb(m.traffic.quota_gb)}`
            : formatGb(m.traffic.used_gb)
          : '—'
      ),
    },
    {
      label: '月底预计（M1）',
      values: machines.map((m) =>
        m.traffic?.projection ? `${m.traffic.projection.projected_gb} GB` : '—'
      ),
      // 只有「照这个速度会超」才上色；预测本身是中性信息
      tones: machines.map((m) =>
        m.traffic?.projection?.over_quota ? 'text-amber-700' : undefined
      ),
    },
    {
      label: '磁盘日增（M2）',
      values: machines.map((_m, i) =>
        trends[i] ? `${trends[i]!.daily_gb > 0 ? '+' : ''}${trends[i]!.daily_gb} GB/天` : '—'
      ),
    },
    {
      label: '预计写满（M2）',
      values: machines.map((_m, i) =>
        trends[i] ? (trends[i]!.days_to_full != null ? `${trends[i]!.days_to_full} 天` : '暂不预测') : '—'
      ),
      tones: machines.map((_m, i) => (trends[i]?.days_to_full != null ? 'text-amber-700' : undefined)),
    },
    // 价格文案直接复用卡片那份（含币种符号与周期后缀）：另写一份必然漂
    { label: '价格', values: machines.map((m) => priceText(m.renewal) || '—') },
    {
      label: '到期',
      values: machines.map((m) =>
        m.renewal?.days_left != null ? `剩余 ${m.renewal.days_left} 天` : '长期'
      ),
    },
  ];
}

interface Props {
  open: boolean;
  machines: Machine[];
  systemByMachine: Record<string, MonitorSystem>;
  /** M2 趋势；未加载时对比表里那两行显示 — */
  capacity: CapacityPayload | null;
  onOpenChange: (open: boolean) => void;
}

/**
 * M3 机器临时对比（设计 §7 Track M：最多 4 台、不做永久布局编辑器）。
 *
 * **选择刻意不持久化**：这是「现在想比一下这两台」的一次性动作，
 * 存进 localStorage 只会让下次打开时出现一组自己都忘了为什么选的机器。
 * 关闭对话框即清空。
 */
export function MachineCompare({ open, machines, systemByMachine, capacity, onOpenChange }: Props) {
  const [selected, setSelected] = useState<string[]>([]);

  useEffect(() => {
    if (!open) setSelected([]);
  }, [open]);

  const toggle = (id: string) => {
    setSelected((prev) =>
      prev.includes(id)
        ? prev.filter((x) => x !== id)
        : prev.length >= COMPARE_LIMIT
          ? prev
          : [...prev, id]
    );
  };

  // 按选择顺序排列，不按台账顺序：用户先点的那台是参照物，应该在最左边
  const picked = selected
    .map((id) => machines.find((m) => m.id === id))
    .filter((m): m is Machine => m != null);
  const rows = buildCompareRows(picked, systemByMachine, capacity);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>机器对比</DialogTitle>
          <DialogDescription>
            最多选 {COMPARE_LIMIT} 台并排看。选择只在本次对话框内有效，关闭即清空。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-wrap gap-1.5">
          {machines.map((m) => {
            const on = selected.includes(m.id);
            const full = !on && selected.length >= COMPARE_LIMIT;
            return (
              <button
                key={m.id}
                type="button"
                disabled={full}
                onClick={() => toggle(m.id)}
                className={cn(
                  'rounded-full border px-3 py-1 text-xs transition-colors',
                  on
                    ? 'border-primary bg-primary/10 text-primary'
                    : 'border-border/60 text-muted-foreground hover:border-primary/50',
                  full && 'cursor-not-allowed opacity-40 hover:border-border/60'
                )}
                title={full ? `最多同时对比 ${COMPARE_LIMIT} 台` : undefined}
              >
                {m.label}
              </button>
            );
          })}
        </div>

        {picked.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">先从上面选机器。</p>
        ) : (
          <div className="max-h-[60vh] overflow-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-background">
                <tr className="border-b text-xs uppercase text-muted-foreground">
                  <th className="px-2 py-1.5 text-left font-medium">指标</th>
                  {picked.map((m) => (
                    <th key={m.id} className="px-2 py-1.5 text-left font-medium">
                      {m.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.label} className="border-b last:border-0">
                    <td className="whitespace-nowrap px-2 py-1.5 text-xs text-muted-foreground">
                      {row.label}
                    </td>
                    {row.values.map((value, i) => (
                      <td
                        key={picked[i].id}
                        className={cn('px-2 py-1.5 font-mono text-xs', row.tones?.[i])}
                        title={value}
                      >
                        {value}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="flex justify-end">
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
