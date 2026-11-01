import { usePluginVisible } from '@/lib/plugins/provider';
import { displayTime } from '@/lib/display-time';
import { useEffect, useState } from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { getOpsStatus } from '@/lib/api';
import type { OpsLastDeploy, OpsStatus, OpsTimedBackup, OpsTimedBackupState } from '@/lib/types';

const EMPTY: OpsStatus = {
  running_commit: null,
  versions_last_checked: null,
  feed_last_fetched: null,
  last_backup_tag: null,
  last_deploy: null,
  last_probe_run: null,
  timed_backup: null,
};

/** null / 空串 →「未知」；不抛错。 */
export function formatOpsValue(value: string | null | undefined): string {
  if (value == null) return '未知';
  const text = String(value).trim();
  return text || '未知';
}

/** `last_deploy` 卡片摘要：优先「短 commit · 部署时间」，只有其中一项就显示那一项；
 * 对象存在但 commit/deployed_at 都拿不到时仍显示未知，不把空对象伪装成有效部署记录。 */
export function formatLastDeploy(value: OpsLastDeploy | null | undefined): string {
  if (!value) return '未知';
  const commitText = formatOpsValue(value.commit);
  const deployedText = value.deployed_at?.trim() ? displayTime(value.deployed_at).text : '未知';
  if (commitText === '未知' && deployedText === '未知') return '未知';
  const shortCommit = commitText === '未知' ? null : commitText.slice(0, 7);
  if (shortCommit && deployedText !== '未知') return `${shortCommit} · ${deployedText}`;
  return shortCommit ?? deployedText;
}

/** `last_deploy` 完整可访问文本（进 `title`）：四键逐一列出；`migrations_applied` 用
 * `== null` 判断而非真值判断，`0` 是合法迁移数不当未知处理。 */
export function describeLastDeploy(value: OpsLastDeploy | null | undefined): string {
  if (!value) return '未知';
  const migrations = value.migrations_applied == null ? '未知' : String(value.migrations_applied);
  return [
    `commit: ${formatOpsValue(value.commit)}`,
    `backup_tag: ${formatOpsValue(value.backup_tag)}`,
    `deployed_at: ${formatOpsValue(value.deployed_at)}`,
    displayTime(value.deployed_at).title,
    `migrations_applied: ${migrations}`,
  ].join('\n');
}

const TIMED_STATE_LABELS: Record<OpsTimedBackupState, string> = {
  ok: '正常',
  stale: '陈旧',
  failed: '失败',
  unknown: '未知',
  never: '从未',
};

/** F17 定时备份摘要：状态中文（失败带阶段）+ 已知成功时间；
 * 没有状态或状态未知一律「未知」，不用当前时间顶替。 */
export function formatTimedBackup(value: OpsTimedBackup | null | undefined): string {
  if (!value) return '未知';
  const label = TIMED_STATE_LABELS[value.state] ?? '未知';
  if (label === '未知') return '未知';
  if (value.state === 'never') return label;
  const when = value.last_success ? displayTime(value.last_success, { minute: true }).text : null;
  let head = label;
  if (value.state === 'failed') {
    const stage = formatOpsValue(value.stage);
    if (stage !== '未知') head = `${label}(${stage})`;
  }
  return when ? `${head} · ${when}` : head;
}

/** F17 完整可访问文本（进 `title`）：五键逐一列出，缺失键显示未知。 */
export function describeTimedBackup(value: OpsTimedBackup | null | undefined): string {
  if (!value) return '未知';
  return [
    `state: ${value.state}`,
    `last_attempt: ${formatOpsValue(value.last_attempt)}`,
    `last_success: ${formatOpsValue(value.last_success)}`,
    displayTime(value.last_success).title,
    `snapshot: ${formatOpsValue(value.snapshot)}`,
    `stage: ${formatOpsValue(value.stage)}`,
  ].join('\n');
}

const FIELDS: { key: keyof OpsStatus; label: string }[] = [
  { key: 'running_commit', label: '运行 commit' },
  { key: 'versions_last_checked', label: '版本采集' },
  { key: 'feed_last_fetched', label: 'Feed 新鲜度' },
  { key: 'last_backup_tag', label: '最近备份' },
  { key: 'timed_backup', label: '定时备份' },
  { key: 'last_deploy', label: '最近部署' },
  { key: 'last_probe_run', label: '探针巡检' },
];

interface Props {
  refreshKey?: number;
  /** 测试注入，跳过网络 */
  status?: OpsStatus;
}

export function OpsStatusCard({ refreshKey = 0, status: injected }: Props) {
  const showFeed = usePluginVisible('feed');
  const [status, setStatus] = useState<OpsStatus>(injected ?? EMPTY);

  useEffect(() => {
    if (injected) {
      setStatus(injected);
      return;
    }
    let cancelled = false;
    getOpsStatus()
      .then((data) => {
        if (!cancelled) setStatus(data);
      })
      .catch(() => {
        if (!cancelled) setStatus(EMPTY);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, injected]);

  return (
    <Card>
      <CardContent className="py-3">
        <div className="mb-2 text-sm font-medium">运维状态</div>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {FIELDS.filter((field) => field.key !== 'feed_last_fetched' || showFeed).map(({ key, label }) => {
            let displayText: string;
            let titleText: string;
            if (key === 'last_deploy') {
              displayText = formatLastDeploy(status.last_deploy);
              titleText = describeLastDeploy(status.last_deploy);
            } else if (key === 'timed_backup') {
              displayText = formatTimedBackup(status.timed_backup);
              titleText = describeTimedBackup(status.timed_backup);
            } else if (key === 'versions_last_checked' || key === 'feed_last_fetched' || key === 'last_probe_run') {
              const time = displayTime(status[key], { assumeUtc: key !== 'last_probe_run' });
              displayText = status[key] ? time.text : '未知';
              titleText = time.title;
            } else {
              displayText = formatOpsValue(status[key] as string | null);
              titleText = displayText;
            }
            return (
              <div key={key} className="min-w-0 rounded-md bg-muted/40 px-2.5 py-2">
                <div className="text-xs text-muted-foreground">{label}</div>
                <div className="mt-0.5 truncate font-mono text-sm" title={titleText}>
                  {displayText}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
