import { DisplayTime } from '@/components/ui/display-time';
import { displayTime } from '@/lib/display-time';
import { useEffect, useState } from 'react';
import { Globe } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { getEntryTraffic } from '@/lib/api';
import type { EntryTrafficStatus } from '@/lib/types';

const UNAVAILABLE: EntryTrafficStatus = {
  available: false,
  sampled_at: null,
  error: null,
  upstreams: [],
  metrics_available: false,
  metrics_error: null,
  counting_since: null,
  hosts: [],
};

/** 失败数染色：0 弱化、>0 红（入口失败即值得看一眼）。 */
export function failsTone(fails: number | null): string {
  if (fails == null) return 'text-muted-foreground';
  return fails > 0 ? 'text-signal-5' : 'text-muted-foreground';
}

/** 0 是合法读数（空闲），只有缺字段 / 解析不出来才是 `—`。 */
export function requestsText(n: number | null): string {
  return n == null ? '—' : String(n);
}

/** 累计量可能上万，卡片宽度有限：≥10000 起走 k/M，其余原样。 */
export function compactCount(n: number): string {
  if (!Number.isFinite(n)) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** `_other` 是 Caddy 的 catchall 桶，直接显示会让人以为有台叫 _other 的机器。 */
export function hostLabel(host: string): string {
  return host === '_other' ? '其它（Host 不匹配）' : host;
}

const ACTIVE_HINT =
  '当前活跃（in-flight）请求数：Caddy 的 num_requests 是瞬时量，请求结束即减回，不是累计请求量';
const FAILS_HINT =
  '被动健康检查「记住」的失败数：未配置 passive health checks 或 fail_duration=0 时恒为 0，不等于没有失败';
const TOTAL_HINT =
  '按域名累计的请求数（Caddy Prometheus 指标 caddy_http_requests_total）。计数器在每次 Caddy 重载时归零，所以是「自上次重载起」的累计量';
const OTHER_HINT =
  'Host 头不匹配任何站点的请求，通常是扫描器或按 IP 直连；后端还会把裸 IP 形态的 host 折叠到这里';

interface Props {
  refreshKey?: number;
  /** 测试注入，跳过网络 */
  status?: EntryTrafficStatus;
}

/**
 * C11：入口上游小卡（aly Caddy admin 经 NAS crontab 导出文件，600s TTL）。
 * upstream 的 address（tailnet IP）不出后端，这里只拿 name/活跃请求数/失败数；
 * name 匹配不到时后端给 `upstream-N`。导出文件缺失 → available=false 显式降级。
 *
 * **口径（2026-09-07 复核，详见 `caddy_view.py` 模块 docstring）**：两个数都是
 * **瞬时量**——`num_requests` 是采样那一刻的活跃请求数（不是累计请求量），
 * `fails` 只在 Caddy 配了被动健康检查时才计数。5 分钟一次的采样几乎总是落在空闲期，
 * 所以**全 0 是正常态，不代表入口没在服务流量**。标题从「入口流量」改成「入口上游」、
 * 数值加「活跃 / 失败」前缀与脚注，都是为了不让这张卡被读成流量统计。
 */
export function EntryTrafficCard({ refreshKey = 0, status: injected }: Props) {
  const [status, setStatus] = useState<EntryTrafficStatus>(injected ?? UNAVAILABLE);

  useEffect(() => {
    if (injected) {
      setStatus(injected);
      return;
    }
    let cancelled = false;
    getEntryTraffic()
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
          <div className="flex shrink-0 items-center gap-1.5 text-sm font-medium">
            <Globe className="h-3.5 w-3.5 text-muted-foreground" />
            入口上游
          </div>
          <div
            className="min-w-0 break-all text-right text-xs text-muted-foreground"
            title={displayTime(status.sampled_at).title}
          >
            {status.available ? <>接口读取 <DisplayTime value={status.sampled_at} /></> : '数据不可用'}
          </div>
        </div>
        {status.upstreams_sampled_at ? (
          <p className="mb-1 break-all text-[11px] text-muted-foreground">
            上游采集：<DisplayTime value={status.upstreams_sampled_at} />
          </p>
        ) : null}
        {status.available && status.upstreams.length > 0 ? (
          <div className="space-y-1">
            {status.upstreams.map((u) => (
              <div
                key={u.id}
                className="flex items-center justify-between gap-2 text-xs"
              >
                <span className="truncate text-foreground/80">{u.name}</span>
                <span className="flex items-center gap-3 font-mono tabular-nums">
                  <span className="text-muted-foreground" title={ACTIVE_HINT}>
                    活跃 {requestsText(u.num_requests)}
                  </span>
                  <span className={failsTone(u.fails)} title={FAILS_HINT}>
                    失败 {requestsText(u.fails)}
                  </span>
                </span>
              </div>
            ))}
            {/* 全 0 是常态，不写清楚就会被当成「采集坏了」 */}
            <p className="pt-1 text-[11px] leading-tight text-muted-foreground">
              瞬时快照：采样那一刻的活跃请求数，非累计流量，空闲时为 0
            </p>
          </div>
        ) : (
          <div className="py-1 text-xs text-muted-foreground">
            {status.error ?? '暂无入口流量数据'}
          </div>
        )}

        {/* 累计量：独立数据链、独立降级，所以是并列分区而不是嵌在上面那块里 */}
        <div className="mt-2 border-t border-border/60 pt-2">
          <div className="mb-1 flex items-baseline justify-between gap-2">
            <span className="text-xs font-medium text-foreground/80">按域名累计</span>
            {status.metrics_available && status.counting_since ? (
              <span
                className="min-w-0 break-all text-right text-[11px] text-muted-foreground"
                title={`计数器在 Caddy 重载时归零，此为本次计数起点：${status.counting_since}`}
              >
                自 {status.counting_since} 起
              </span>
            ) : null}
          </div>
          {status.metrics_sampled_at ? (
            <p className="mb-1 break-all text-[11px] text-muted-foreground">
              指标采集：<DisplayTime value={status.metrics_sampled_at} />
            </p>
          ) : null}
          {status.metrics_available && status.hosts.length > 0 ? (
            <div className="space-y-1">
              {status.hosts.map((h) => (
                <div
                  key={h.host}
                  className="flex items-center justify-between gap-2 text-xs"
                >
                  <span
                    className="truncate text-foreground/80"
                    title={h.host === '_other' ? OTHER_HINT : h.host}
                  >
                    {hostLabel(h.host)}
                  </span>
                  <span className="flex items-center gap-3 font-mono tabular-nums">
                    <span className="text-muted-foreground" title={TOTAL_HINT}>
                      {compactCount(h.requests_total)}
                    </span>
                    <span
                      className={
                        h.server_5xx > 0 ? 'text-signal-5' : 'text-muted-foreground'
                      }
                      title={`4xx ${h.client_4xx} / 5xx ${h.server_5xx}`}
                    >
                      5xx {compactCount(h.server_5xx)}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="py-0.5 text-xs text-muted-foreground">
              {status.metrics_error ?? '暂无累计数据'}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
