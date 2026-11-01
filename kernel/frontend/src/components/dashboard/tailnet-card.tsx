import { DisplayTime } from '@/components/ui/display-time';
import { displayTime } from '@/lib/display-time';
import { useEffect, useMemo, useState } from 'react';
import { ArrowUpCircle, Settings2 } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { MiniMenu } from '@/components/dashboard/mini-menu';
import {
  TAILNET_FIELD_DEFS,
  readTailnetFields,
  writeTailnetFields,
} from '@/components/dashboard/tailnet-fields';
import type { TailnetFieldKey } from '@/components/dashboard/tailnet-fields';
import { getTailnet } from '@/lib/api';
import { cn } from '@/lib/utils';
import type { TailnetMachine, TailnetStatus } from '@/lib/types';

const UNAVAILABLE: TailnetStatus = {
  available: false,
  sampled_at: null,
  error: null,
  machines: [],
};

const ALL_FIELD_KEYS: TailnetFieldKey[] = TAILNET_FIELD_DEFS.map((d) => d.key);

/** key 剩余天数 → 展示文案；null = 不过期/未知。 */
export function keyExpiryText(days: number | null): string {
  if (days == null) return '不过期';
  if (days < 0) return `已过期 ${-days} 天`;
  return `剩 ${days} 天`;
}

/** key 剩余天数 → 色阶：已过期红、≤7 天警告、其余弱化（与 attention 7 天窗口同口径）。 */
export function keyExpiryTone(days: number | null): string {
  if (days == null) return 'text-muted-foreground';
  if (days < 0) return 'text-signal-5';
  if (days <= 7) return 'text-signal-4';
  return 'text-muted-foreground';
}

/** 在线点颜色：与 machine-card statusTone 同色板；null（未找到 peer）走 muted。 */
export function onlineDotTone(online: boolean | null): string {
  if (online === true) return 'bg-signal-1';
  if (online === false) return 'bg-signal-5';
  return 'bg-muted-foreground';
}

export function onlineSummary(machines: TailnetMachine[]): string {
  const known = machines.filter((m) => m.online != null);
  const online = known.filter((m) => m.online === true).length;
  return `${online}/${machines.length} 在线`;
}

interface Props {
  refreshKey?: number;
  /** 测试注入，跳过网络 */
  status?: TailnetStatus;
  /** 测试注入，跳过 localStorage */
  initialFields?: TailnetFieldKey[] | null;
}

export function TailnetCard({ refreshKey = 0, status: injected, initialFields }: Props) {
  const [status, setStatus] = useState<TailnetStatus>(injected ?? UNAVAILABLE);
  // fields 白名单：null = 没设过 → 默认全显（D4，C2 增强的统一字段开关设计）
  const [fields, setFields] = useState<TailnetFieldKey[] | null>(
    () => initialFields ?? readTailnetFields()
  );
  const visible = useMemo(() => new Set(fields ?? ALL_FIELD_KEYS), [fields]);

  const toggleField = (key: TailnetFieldKey) => {
    setFields((prev) => {
      const base = new Set(prev ?? ALL_FIELD_KEYS);
      if (base.has(key)) base.delete(key);
      else base.add(key);
      const next = ALL_FIELD_KEYS.filter((k) => base.has(k));
      writeTailnetFields(next);
      return next;
    });
  };

  useEffect(() => {
    if (injected) {
      setStatus(injected);
      return;
    }
    let cancelled = false;
    getTailnet()
      .then((data) => {
        if (!cancelled) setStatus(data);
      })
      .catch(() => {
        if (!cancelled) setStatus(UNAVAILABLE);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, injected]);

  return (
    <Card>
      <CardContent className="py-3">
        <div className="mb-2 flex items-baseline justify-between gap-2">
          <div className="text-sm font-medium">Tailnet 组网</div>
          <div className="flex items-center gap-2">
            {status.available && status.machines.length > 0 && (
              <div
                className="text-xs text-muted-foreground"
                title={status.sampled_at ? `接口读取：${displayTime(status.sampled_at).title}` : undefined}
              >
                {onlineSummary(status.machines)}
              </div>
            )}
            <MiniMenu
              label="显示字段"
              trigger={
                <>
                  <Settings2 className="h-3.5 w-3.5" />
                  字段
                </>
              }
              items={TAILNET_FIELD_DEFS.map((def) => ({
                key: def.key,
                label: `${visible.has(def.key) ? '✓' : '·'} ${def.label}`,
                onSelect: () => toggleField(def.key),
              }))}
            />
          </div>
        </div>
        {status.source_sampled_at ? (
          <p className="mb-2 break-all text-[11px] text-muted-foreground">
            源采集：<DisplayTime value={status.source_sampled_at} />
          </p>
        ) : null}
        {!status.available ? (
          <p className="text-xs text-muted-foreground">
            Tailnet 数据不可用{status.error ? `：${status.error}` : ''}
          </p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-6">
            {status.machines.map((m) => {
              const titleParts = [
                m.hostname,
                m.client_version,
                m.os,
                // authorized 只进 tooltip：false（待授权）值得注意，true 不值得占一列
                m.authorized === false ? '未授权' : null,
              ].filter(Boolean);
              return (
                <div
                  key={m.machine_id}
                  className="flex min-w-0 items-center gap-2 rounded-md bg-muted/40 px-2.5 py-2"
                  title={titleParts.length > 0 ? titleParts.join(' · ') : undefined}
                >
                  <span
                    className={cn('h-2.5 w-2.5 shrink-0 rounded-full', onlineDotTone(m.online))}
                  />
                  <span className="truncate font-mono text-sm">{m.machine_id}</span>
                  {visible.has('os') && m.os && (
                    <span className="shrink-0 text-xs text-muted-foreground">{m.os}</span>
                  )}
                  {visible.has('update') && m.update_available === true && (
                    <span
                      className="shrink-0 text-signal-4"
                      title={`有新版本可用（当前 ${m.client_version ?? '未知'}）`}
                    >
                      <ArrowUpCircle className="h-3.5 w-3.5" />
                      <span className="sr-only">有新版本</span>
                    </span>
                  )}
                  {visible.has('expiry') && (
                    <span
                      className={cn(
                        'ml-auto shrink-0 text-xs tabular-nums',
                        keyExpiryTone(m.key_expiry_days)
                      )}
                    >
                      {keyExpiryText(m.key_expiry_days)}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
