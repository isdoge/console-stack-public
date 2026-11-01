import { ChevronRight } from 'lucide-react';
import type { ServiceEntry } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * C5 → L4「入口维度」：把一个服务的入口链摊成 `域名 › 边缘 › tailnet › 内网`。
 *
 * 与既有「访问」列（H｜T｜I ✅❌?）**不重复**：那一列答的是「通不通」，
 * 这一列答的是「谁在前面」——公网请求先落到哪个边缘、再往内网走哪一层。
 * 数据全部来自后端 `entry_view` 的零探测投影：域名与三个布尔位，
 * **没有任何内网地址**（tailnet / 家庭 IP 不出后端，见 e329e27 纪律）。
 *
 * 边缘层有三态：`caddy`（在 upstream 名单里）、无此层（不在名单里）、
 * **未知**（`edge_known=false`，C11 的 cron 快照不可用）——未知显式标出来，
 * 不假装成「没有边缘」。
 */

/** 域名只显示主机名：一行表格里 `https://…` 前缀纯属噪音。 */
export function domainHost(url: string | null): string | null {
  if (!url) return null;
  const text = url.trim();
  if (!text) return null;
  const withScheme = text.includes('://') ? text : `https://${text}`;
  try {
    return new URL(withScheme).host || null;
  } catch {
    return text;
  }
}

function Step({
  label,
  title,
  tone = 'muted',
  href,
}: {
  label: string;
  title: string;
  tone?: 'primary' | 'muted' | 'unknown';
  href?: string;
}) {
  const cls = cn(
    'whitespace-nowrap',
    tone === 'primary' && 'text-foreground',
    tone === 'muted' && 'text-muted-foreground',
    tone === 'unknown' && 'text-muted-foreground/60 italic'
  );
  if (href) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(cls, 'hover:underline')}
        title={title}
      >
        {label}
      </a>
    );
  }
  return (
    <span className={cls} title={title}>
      {label}
    </span>
  );
}

interface Props {
  entry?: ServiceEntry;
  className?: string;
}

export function EntryChain({ entry, className }: Props) {
  if (!entry) return <span className="text-xs text-muted-foreground">-</span>;

  const steps: React.ReactNode[] = [];
  const host = domainHost(entry.domain);
  if (host) {
    steps.push(
      <Step
        key="domain"
        label={host}
        title={`公网域名：${entry.domain}`}
        tone="primary"
        href={entry.domain ?? undefined}
      />
    );
  }
  if (entry.edge) {
    steps.push(
      <Step key="edge" label={entry.edge} title="边缘：在 aly Caddy 的 upstream 名单里" />
    );
  } else if (host && !entry.edge_known) {
    // 有域名却拿不到边缘数据 → 显式未知（cron 快照缺失），不装作直连
    steps.push(<Step key="edge" label="边缘未知" title="Caddy upstream 快照不可用（cron 未就绪）" tone="unknown" />);
  }
  if (entry.tailscale) {
    steps.push(<Step key="tailscale" label="tailnet" title="内网入口：Tailscale" />);
  }
  if (entry.home) {
    steps.push(<Step key="home" label="内网" title="内网入口：家庭局域网" />);
  }

  if (steps.length === 0) {
    return (
      <span className="text-xs text-muted-foreground" title="台账未登记任何访问入口">
        -
      </span>
    );
  }

  return (
    <div className={cn('flex items-center gap-0.5 text-xs', className)}>
      {steps.map((step, i) => (
        <span key={i} className="flex items-center gap-0.5">
          {i > 0 && <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground/50" />}
          {step}
        </span>
      ))}
    </div>
  );
}
