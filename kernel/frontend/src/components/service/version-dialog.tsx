import { DisplayTime } from '@/components/ui/display-time';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { getVersionDetail,
  saveUpgradeReview,
  apiErrorMessage,
} from '@/lib/api';
import { useEventCallback } from '@/lib/use-event-callback';
import type { UpgradePrep, VersionDetail } from '@/lib/types';
import { displayVersion } from '@/lib/version-display';

interface Props {
  open: boolean;
  name: string;
  onOpenChange: (open: boolean) => void;
}

export function VersionDialog({ open, name, onOpenChange }: Props) {
  const [detail, setDetail] = useState<VersionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const pending = useRef(false);

  const load = useEventCallback(async (sync: boolean = false) => {
    if (!open || !name || pending.current) return;
    const current = ++generation.current;
    pending.current = true;
    setLoading(true);
    setError('');
    try {
      const res = await getVersionDetail(name, sync);
      if (generation.current !== current) return;
      if (res.error) setError(res.error);
      else setDetail(res);
    } catch (err: unknown) {
      if (generation.current === current) setError(apiErrorMessage(err));
    } finally {
      if (generation.current === current) {
        pending.current = false;
        setLoading(false);
      }
    }
  });

  useEffect(() => {
    pending.current = false;
    setError('');
    setDetail(null);
    setLoading(false);
    if (open && name) {
      load(false);
    }
    return () => { generation.current++; };
  }, [open, name, load]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-3xl flex-col overflow-hidden p-4 sm:p-6">
        <DialogHeader className="shrink-0 pr-6">
          <DialogTitle className="break-all">版本对比 · {name}</DialogTitle>
          <DialogDescription>
            {detail?.track_upstream === false
              ? '自部署服务，仅展示本机版本。'
              : '本机版本与上游 GitHub Release 的差异。'}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain pr-1">
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          {loading && !detail ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : detail ? (
            <div className="space-y-3 text-sm">
              <VersionSection title="版本概览" defaultOpen>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Cell label="本机版本" value={displayVersion(detail.local, '未知')} mono />
                  <Cell label="上游（GitHub Release）" value={displayVersion(detail.upstream, '未知')} mono />
                  <Cell
                    label="状态"
                    value={detail.status || (detail.outdated ? '可更新' : '未知')}
                  />
                </div>
                {detail.compare_url && (
                  <a
                    href={detail.compare_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-3 inline-block text-sm text-primary hover:underline"
                  >
                    在 GitHub 查看 →
                  </a>
                )}
              </VersionSection>

              {detail.changes && detail.changes.length > 0 && (
                <VersionSection title="版本区间更新" summary={`${detail.changes.length} 条`}>
                  <p className="mb-2 break-all text-xs text-muted-foreground">{displayVersion(detail.local, '未知')} → {displayVersion(detail.upstream, '未知')}</p>
                  <ul className="space-y-1 break-words rounded-md border bg-muted/30 p-3 text-xs">
                    {detail.changes.map((c) => (
                      <li key={c.sha} className="leading-relaxed">
                        <span className="mr-2 font-mono text-violet-500">{c.sha}</span>
                        {c.msg}
                      </li>
                    ))}
                  </ul>
                </VersionSection>
              )}

              {detail.release_notes && (
                <VersionSection title="最新 Release 说明">
                  <pre className="whitespace-pre-wrap break-words rounded-md border bg-muted/30 p-3 text-xs">
                    {detail.release_notes}
                  </pre>
                </VersionSection>
              )}

              {detail.upgrade_prep && (
                <UpgradePrepSection
                  name={name}
                  prep={detail.upgrade_prep}
                  onSaved={(review) =>
                    setDetail((cur) =>
                      cur?.upgrade_prep
                        ? { ...cur, upgrade_prep: { ...cur.upgrade_prep, review } }
                        : cur,
                    )
                  }
                />
              )}

              {detail.last_checked && (
                <p className="text-xs text-muted-foreground">上次检查：<DisplayTime value={detail.last_checked} assumeUtc /></p>
              )}
            </div>
          ) : null}
        </div>

        <DialogFooter className="shrink-0 gap-2 sm:gap-0">
          <Button variant="outline" onClick={() => load(true)} disabled={loading}>
            刷新同步
          </Button>
          <Button onClick={() => onOpenChange(false)}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function VersionSection({ title, summary, defaultOpen = false, children }: {
  title: string;
  summary?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(defaultOpen);
  const id = useId();

  return (
    <section className="min-w-0 rounded-md border">
      <button
        id={`${id}-trigger`}
        type="button"
        aria-expanded={expanded}
        aria-controls={id}
        onClick={() => setExpanded((current) => !current)}
        className="flex w-full items-center gap-2 rounded-md p-3 text-left hover:bg-muted/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
      >
        <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-xs font-semibold">{title}</span>
          {summary && <span className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">{summary}</span>}
        </span>
        <ChevronDown aria-hidden="true" className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${expanded ? 'rotate-180' : ''}`} />
      </button>
      <div id={id} role="region" aria-labelledby={`${id}-trigger`} hidden={!expanded} className="space-y-3 px-3 pb-3">
        {children}
      </div>
    </section>
  );
}

const REVIEW_STATUSES: Array<{ value: UpgradePrep['review']['status']; label: string }> = [
  { value: 'pending', label: '待审阅' },
  { value: 'prepared', label: '已就绪' },
  { value: 'deferred', label: '延后' },
  { value: 'ignored', label: '忽略' },
];

function UpgradePrepSection({ name, prep, onSaved }: {
  name: string;
  prep: UpgradePrep;
  onSaved: (review: UpgradePrep['review']) => void;
}) {
  const [checks, setChecks] = useState<Record<string, boolean>>(prep.review.checks || {});
  const [status, setStatus] = useState(prep.review.status);
  const [reason, setReason] = useState(prep.review.reason || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  // 换服务或父级刷新后，以最新持久化状态重置表单
  useEffect(() => {
    setChecks(prep.review.checks || {});
    setStatus(prep.review.status);
    setReason(prep.review.reason || '');
    setError('');
  }, [name, prep.review]);

  const dirty =
    status !== prep.review.status ||
    reason !== (prep.review.reason || '') ||
    prep.checks.some((c) => Boolean(checks[c.key]) !== c.done);

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      const review = await saveUpgradeReview(name, { status, reason, checks });
      onSaved(review);
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <VersionSection title="升级影响与准备" summary={<>
      <span className={
        prep.category === 'breaking'
          ? 'rounded bg-destructive/15 px-1.5 text-xs font-medium text-destructive'
          : prep.category === 'security'
            ? 'rounded bg-amber-500/15 px-1.5 text-xs font-medium text-amber-600 dark:text-amber-400'
            : 'rounded bg-muted px-1.5 text-xs font-medium text-muted-foreground'
      }>
        {prep.category === 'breaking' ? '含破坏性变更候选' : prep.category === 'security' ? '含安全修复' : '常规变更'}
      </span>
      <span className={
        prep.readiness === 'preparation_reviewed'
          ? 'rounded bg-emerald-500/15 px-1.5 text-xs font-medium text-emerald-600 dark:text-emerald-400'
          : 'rounded bg-muted px-1.5 text-xs font-medium text-muted-foreground'
      }>
        {prep.readiness === 'preparation_reviewed' ? '准备已审阅' : '待审阅'}
      </span>
    </>}>
      {prep.blockers.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">
          {prep.blockers.map((b) => <li key={b}>{b}</li>)}
        </ul>
      )}

      {prep.changes.length > 0 && (
        <ul className="space-y-1 break-words rounded-md border bg-muted/30 p-3 text-xs">
          {prep.changes.map((c) => (
            <li key={`${c.line}:${c.category}`} className="leading-relaxed">
              <span className={
                c.severity === 'breaking'
                  ? 'mr-2 rounded bg-destructive/15 px-1 font-medium text-destructive'
                  : 'mr-2 rounded bg-amber-500/15 px-1 font-medium text-amber-600 dark:text-amber-400'
              }>
                {c.category}
              </span>
              {c.excerpt}
            </li>
          ))}
        </ul>
      )}

      <div className="space-y-1.5">
        {prep.checks.map((c) => (
          <label key={c.key} className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={Boolean(checks[c.key])}
              onChange={(e) => setChecks((cur) => ({ ...cur, [c.key]: e.target.checked }))}
            />
            <span>{c.label}</span>
          </label>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="审阅状态"
          className="h-8 rounded-md border bg-background px-2 text-xs"
          value={status}
          onChange={(e) => setStatus(e.target.value as UpgradePrep['review']['status'])}
        >
          {REVIEW_STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
        {(status === 'deferred' || status === 'ignored' || reason) && (
          <input
            aria-label="理由"
            className="h-8 min-w-40 flex-1 rounded-md border bg-background px-2 text-xs"
            placeholder="忽略或延后必须填写理由"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        )}
        <Button size="sm" onClick={save} disabled={saving || !dirty}>
          {saving ? '保存中…' : '保存审阅'}
        </Button>
      </div>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {prep.review.updated_at && (
        <p className="text-xs text-muted-foreground">
          上次保存：<DisplayTime value={prep.review.updated_at} assumeUtc />
        </p>
      )}
    </VersionSection>
  );
}

function Cell({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={mono ? 'break-all font-mono text-sm' : 'break-words text-sm'}>{value}</div>
    </div>
  );
}
