import type { Service } from '@/lib/types';
import { cn } from '@/lib/utils';
import { displayVersion } from '@/lib/version-display';

interface Props {
  svc: Service;
  onOpen?: (svc: Service) => void;
}

const UNKNOWN_VERSIONS = new Set([
  '',
  'n/a',
  'unknown',
  '未知',
  'latest',
  'running (docker)',
]);

export function isKnownVersion(value?: string): boolean {
  return !UNKNOWN_VERSIONS.has((value || '').trim().toLowerCase());
}

export function versionsDiffer(local?: string, upstream?: string): boolean {
  if (!isKnownVersion(local) || !isKnownVersion(upstream)) return false;
  return local!.replace(/^v/i, '') !== upstream!.replace(/^v/i, '');
}

export function VersionCell({ svc, onOpen }: Props) {
  const local = svc.version_info?.local || 'N/A';
  const upstream = svc.version_info?.upstream || 'N/A';
  const trackUpstream = svc.version?.track_upstream !== false;
  const outdated = trackUpstream && versionsDiffer(local, upstream);
  const clickable = !!(
    svc.version?.enabled
    || svc.version?.repo_url
    || isKnownVersion(svc.version_info?.upstream)
  );

  // 只展示当前版本；有更新时红色加粗，新版本号点开详情再看
  return (
    <button
      type="button"
      className={cn(
        'min-h-[44px] min-w-[44px] rounded px-2 text-left font-mono text-xs leading-tight focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring md:min-h-0 md:min-w-0 md:px-0',
        clickable ? 'cursor-pointer hover:opacity-80' : 'cursor-default'
      )}
      onClick={() => clickable && onOpen?.(svc)}
      title={outdated ? `有新版本 ${displayVersion(upstream)}，点击查看详情` : clickable ? '点击查看版本详情' : undefined}
      disabled={!clickable}
    >
      <span className={cn(outdated ? 'font-bold text-red-600' : 'text-foreground')}>
        {displayVersion(local)}
      </span>
    </button>
  );
}
