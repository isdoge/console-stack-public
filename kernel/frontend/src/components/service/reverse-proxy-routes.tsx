import { ArrowRight } from 'lucide-react';
import type { ReverseProxyRoute, Service } from '@/lib/types';

export function formatProxyFrontend(route: ReverseProxyRoute): string {
  const listen = route.listen?.length ? route.listen.join(', ') : '';
  const base = route.hosts?.length
    ? `${route.hosts.join(', ')}${listen ? ` (${listen})` : ''}`
    : listen || '未标明监听';
  const paths = route.paths?.length ? route.paths.join(', ') : '/*';
  return `${base} ${paths}`;
}

export function formatProxySource(source?: string): string | null {
  if (source === 'host') return '宿主机';
  if (source === 'container') return '容器';
  return source || null;
}

interface Props {
  svc: Service;
}

export function ReverseProxyRoutes({ svc }: Props) {
  const routes = svc.proxy_routes || [];
  if (routes.length === 0) {
    if (svc.proxy_error) {
      return (
        <span className="text-xs text-destructive" title={svc.proxy_error}>
          读取失败
        </span>
      );
    }
    return <span className="text-xs text-muted-foreground">-</span>;
  }

  return (
    <div className="min-w-0 space-y-1">
      {routes.map((route, index) => {
        const frontend = formatProxyFrontend(route);
        const source = formatProxySource(route.source);
        const upstream = route.upstreams.join(', ');
        return (
          <div
            key={`${frontend}-${upstream}-${index}`}
            className="flex items-center gap-1 whitespace-nowrap font-mono text-xs"
            title={`${source ? `${source}: ` : ''}${frontend} -> ${upstream}`}
          >
            {source && (
              <span className="shrink-0 text-[10px] text-muted-foreground">
                {source}
              </span>
            )}
            <span>{frontend}</span>
            <ArrowRight className="h-3 w-3 shrink-0 text-muted-foreground" />
            <span className="text-muted-foreground">{upstream}</span>
          </div>
        );
      })}
      {svc.proxy_error && (
        <div className="text-xs text-destructive" title={svc.proxy_error}>
          部分读取失败
        </div>
      )}
    </div>
  );
}
