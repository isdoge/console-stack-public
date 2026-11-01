import { useEffect, useState } from 'react';
import { AlertTriangle, ChevronDown } from 'lucide-react';
import type { ObservabilityPayload } from '@/lib/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';

/**
 * 监控事件展示的共享零件：告警/事件名中文化、severity 中文 chip、可折叠卡。
 * 总控台 ObservabilitySummary 与单机 ObservabilityMachine 共用，防两处漂移。
 */

/**
 * Beszel 告警/事件名 → 中文（规则名是 Beszel 里建告警时起的名字，常见为英文）。
 * 键统一小写比；未收录的名字原样返回——宁可显示原文也不编一个错误翻译。
 */
const EVENT_LABELS: Record<string, string> = {
  status: '在线状态',
  cpu: 'CPU 使用率',
  memory: '内存使用率',
  disk: '磁盘使用率',
  usage: '使用率',
  bandwidth: '带宽',
  network: '网络流量',
  temperature: '温度',
  load: '系统负载',
  loadavg1: '1 分钟负载',
  loadavg5: '5 分钟负载',
  loadavg15: '15 分钟负载',
  uptime: '在线时长',
};

export function eventLabel(name: string): string {
  const key = (name || '').trim().toLowerCase();
  return EVENT_LABELS[key] || name || '—';
}

const SEVERITY_LABELS: Record<string, string> = {
  error: '错误',
  warn: '警告',
  warning: '警告',
  info: '信息',
};

export function severityLabel(severity: string): string {
  return SEVERITY_LABELS[(severity || '').toLowerCase()] || severity || '—';
}

export function SeverityChip({ severity }: { severity: string }) {
  const raw = (severity || '').toLowerCase();
  return (
    <span
      className={cn(
        'shrink-0 rounded px-1.5 py-0.5 text-[10px] tracking-wide',
        raw === 'error' ? 'bg-destructive/15 text-destructive' : 'bg-amber-500/15 text-amber-700'
      )}
    >
      {severityLabel(severity)}
    </span>
  );
}

export function monitoringCoverageGaps(
  data: ObservabilityPayload,
  visible?: { systemd?: boolean; storage?: boolean },
): string[] {
  const gaps: string[] = [];
  if (visible?.systemd !== false && data.capabilities?.systemd === 'unsupported') gaps.push('systemd 服务');
  if (visible?.storage !== false && data.capabilities?.smart === 'unsupported') gaps.push('SMART/mdraid');
  return gaps;
}

export function MonitoringCoverageNotice({ gaps }: { gaps: string[] }) {
  if (gaps.length === 0) return null;
  return (
    <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <p>当前 Console PG 未覆盖：{gaps.join('、')}。空列表不代表检查通过。</p>
    </div>
  );
}

function loadOpen(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(`console.collapsible.${key}`);
    return raw == null ? fallback : raw === '1';
  } catch {
    return fallback;
  }
}

interface CollapsibleCardProps {
  /** localStorage 键后缀（自动加 console.collapsible. 前缀），保证唯一即可 */
  storageKey: string;
  title: string;
  /** 折叠时也要看到的摘要（如「告警 1 · 事件 3」），渲染在标题右侧 */
  summary?: React.ReactNode;
  /** 标题右侧的操作/时间等，折叠时仍显示 */
  extras?: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
  /** CardContent 的附加 className（对齐各调用方原来的间距） */
  contentClassName?: string;
}

/**
 * 可折叠卡（点击标题整行切换，状态持久到 localStorage）。
 * 折叠只藏内容，标题/摘要/右上角 extras 恒在——收起后仍能扫一眼有没有事。
 */
export function CollapsibleCard({
  storageKey,
  title,
  summary,
  extras,
  defaultOpen = true,
  children,
  contentClassName,
}: CollapsibleCardProps) {
  const [open, setOpen] = useState(() => loadOpen(storageKey, defaultOpen));
  useEffect(() => {
    try {
      localStorage.setItem(`console.collapsible.${storageKey}`, open ? '1' : '0');
    } catch {
      // localStorage 不可用（隐私模式等）时折叠状态退化为会话内记忆
    }
  }, [open, storageKey]);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <button
          type="button"
          className="flex min-w-0 items-center gap-1.5 rounded text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <CardTitle className="shrink-0 text-base">{title}</CardTitle>
          {summary && (
            <span className="truncate text-xs text-muted-foreground">{summary}</span>
          )}
          <ChevronDown
            className={cn(
              'h-4 w-4 shrink-0 text-muted-foreground transition-transform',
              open && 'rotate-180'
            )}
          />
        </button>
        {extras && <div className="flex shrink-0 items-center gap-2">{extras}</div>}
      </CardHeader>
      {open && <CardContent className={contentClassName}>{children}</CardContent>}
    </Card>
  );
}
