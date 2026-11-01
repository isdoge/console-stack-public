import type { AccessKey, Service } from '@/lib/types';
import { cn } from '@/lib/utils';

// H=家庭 / T=Tailscale / I=公网，与表头「H｜T｜I」一一对应
const COLS: { key: AccessKey; label: string; title: string }[] = [
  { key: 'home', label: 'H', title: '家庭' },
  { key: 'tailscale', label: 'T', title: 'Tailscale' },
  { key: 'domain', label: 'I', title: '公网' },
];

interface Props {
  svc: Service;
}

export function AccessBadges({ svc }: Props) {
  return (
    <div className="flex items-center gap-2 font-mono text-xs">
      {COLS.map((col) => {
        const url = svc.access?.[col.key];
        const probe = svc.access?.probe?.[col.key];

        if (!url) {
          return (
            <span key={col.key} className="w-4 text-center text-muted-foreground" title={`${col.title}: 未配置`}>
              -
            </span>
          );
        }

        const mark = probe === true ? '✅' : probe === false ? '❌' : '?';
        const title = `${col.title}: ${url}${probe === false ? '（探测不通）' : probe == null ? '（未探测）' : ''}`;

        if (probe === false) {
          return (
            <span key={col.key} className="w-4 text-center" title={title}>
              {mark}
            </span>
          );
        }
        return (
          <a
            key={col.key}
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className={cn('w-4 text-center hover:opacity-70', probe == null && 'text-muted-foreground')}
            title={title}
          >
            {mark}
          </a>
        );
      })}
    </div>
  );
}
