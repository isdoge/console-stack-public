import { useEffect, useMemo, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  discoverMachineServices,
  registerDiscoveredServices,
  apiErrorMessage,
} from '@/lib/api';
import type { DiscoveredContainer } from '@/lib/api';

interface RowState {
  checked: boolean;
  portText: string;
}

interface Props {
  open: boolean;
  machineId: string;
  machineLabel: string;
  onOpenChange: (open: boolean) => void;
  /** 登记成功后回调（上层刷新服务列表） */
  onAdded: () => void;
}

/**
 * 扫描未登记服务：后端只读 `docker ps -a`（命令写死，无任意 shell），
 * 前端勾选 + 可选填端口后批量走既有 POST /api/service 登记。
 * 端口留空 → 该服务 health_probe: false（不探测，之后可手动补）。
 */
export function DiscoverDialog({ open, machineId, machineLabel, onOpenChange, onAdded }: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [candidates, setCandidates] = useState<DiscoveredContainer[]>([]);
  const [rows, setRows] = useState<Record<string, RowState>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError('');
    setCandidates([]);
    setRows({});
    discoverMachineServices(machineId)
      .then((res) => {
        if (res.ok === false) {
          setError(res.error || '扫描失败');
          return;
        }
        const list = res.candidates || [];
        setCandidates(list);
        setRows(
          Object.fromEntries(
            list.map((c) => [c.name, { checked: false, portText: suggestPort(c) }])
          )
        );
        if (list.length === 0) toast.success('本机容器都已登记，没有新候选');
      })
      .catch((err) => setError(apiErrorMessage(err) || '扫描失败'))
      .finally(() => setLoading(false));
  }, [open, machineId]);

  const selectedCount = useMemo(
    () => Object.values(rows).filter((r) => r.checked).length,
    [rows]
  );

  /**
   * L4：按 Docker 官方 compose project label 分组（`com.docker.compose.project`），
   * **不靠容器名猜**。没有 label 的归「未分组」并排在最后 —— 2026-09-07 线上实测
   * NAS/TXY/ALY 25 个容器里 21 个带该 label，4 个没有（beszel-agent ×3 + 一个
   * precompose 遗留容器）。分组只影响这里的展示顺序，不写进台账。
   */
  const groups = useMemo(() => {
    const byStack = new Map<string, DiscoveredContainer[]>();
    for (const item of candidates) {
      const key = item.stack || '';
      const list = byStack.get(key);
      if (list) list.push(item);
      else byStack.set(key, [item]);
    }
    return Array.from(byStack.entries()).sort(([a], [b]) => {
      if (a === b) return 0;
      if (a === '') return 1; // 未分组永远垫底
      if (b === '') return -1;
      return a.localeCompare(b);
    });
  }, [candidates]);

  const toggleStack = (items: DiscoveredContainer[], checked: boolean) => {
    setRows((prev) => {
      const next = { ...prev };
      for (const item of items) {
        next[item.name] = { ...(next[item.name] ?? { portText: '' }), checked };
      }
      return next;
    });
  };

  const submit = async () => {
    const items = candidates
      .filter((c) => rows[c.name]?.checked)
      .map((c) => ({
        container: c.name,
        name: c.name,
        port: parsePort(rows[c.name]?.portText),
      }));
    if (items.length === 0) return;
    setSaving(true);
    try {
      const { added, failed } = await registerDiscoveredServices(machineId, items);
      if (added.length > 0) toast.success(`已登记 ${added.length} 项：${added.join('、')}`);
      failed.forEach((f) => toast.error(`${f.name} 登记失败: ${f.error}`));
      if (added.length > 0) {
        onOpenChange(false);
        onAdded();
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>扫描 {machineLabel} 未登记容器</DialogTitle>
          <DialogDescription>
            只读执行 <code className="font-mono text-xs">docker ps -a</code>
            ，对照台账列出未登记容器；勾选后才写入台账。端口留空则不做健康探测。
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> 正在扫描…
          </div>
        ) : error ? (
          <p className="py-4 text-sm text-destructive">{error}</p>
        ) : candidates.length === 0 ? (
          <p className="py-4 text-sm text-muted-foreground">本机容器都已登记。</p>
        ) : (
          <div className="max-h-80 space-y-3 overflow-y-auto pr-1">
            {groups.map(([stack, items]) => {
              const allChecked = items.every((item) => rows[item.name]?.checked);
              return (
                <div key={stack || '__ungrouped__'} className="space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className="text-xs font-medium text-muted-foreground"
                      data-testid="stack-group-label"
                    >
                      {stack ? (
                        <>
                          <span className="font-mono">{stack}</span>
                          <span className="ml-1 font-normal">· compose 栈</span>
                        </>
                      ) : (
                        '未分组（无 compose 标签）'
                      )}
                    </span>
                    {items.length > 1 && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 px-1.5 text-xs"
                        onClick={() => toggleStack(items, !allChecked)}
                      >
                        {allChecked ? '取消整组' : '选中整组'}
                      </Button>
                    )}
                  </div>
                  {items.map((candidate) => (
                    <CandidateRow
                      key={candidate.name}
                      candidate={candidate}
                      row={rows[candidate.name] || { checked: false, portText: '' }}
                      onChange={(next) =>
                        setRows((prev) => ({ ...prev, [candidate.name]: next }))
                      }
                    />
                  ))}
                </div>
              );
            })}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={saving}>
            取消
          </Button>
          <Button
            size="sm"
            onClick={submit}
            disabled={saving || loading || selectedCount === 0}
          >
            {saving && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            登记所选（{selectedCount}）
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CandidateRow({
  candidate,
  row,
  onChange,
}: {
  candidate: DiscoveredContainer;
  row: RowState;
  onChange: (next: RowState) => void;
}) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-border/60 px-2.5 py-2">
      <input
        type="checkbox"
        className="h-3.5 w-3.5 shrink-0 accent-primary"
        checked={row.checked}
        onChange={(e) => onChange({ ...row, checked: e.target.checked })}
        aria-label={`登记 ${candidate.name}`}
      />
      <div className="min-w-0 flex-1">
        <div className="truncate font-mono text-sm" title={candidate.name}>
          {candidate.name}
        </div>
        <div className="truncate text-xs text-muted-foreground" title={candidate.image}>
          {candidate.image || '—'} · {candidate.status || '—'}
        </div>
      </div>
      <input
        type="text"
        inputMode="numeric"
        placeholder="端口"
        className="h-7 w-20 shrink-0 rounded border bg-background px-2 font-mono text-xs"
        value={row.portText}
        onChange={(e) => onChange({ ...row, portText: e.target.value })}
      />
    </div>
  );
}

/** 常见镜像 → 建议健康检查端口；认不出就留空（留空 = 不探测）。 */
function suggestPort(c: DiscoveredContainer): string {
  const image = (c.image || '').toLowerCase();
  if (image.includes('nginx') || image.includes('openresty') || image.includes('caddy')) return '80';
  if (image.includes('redis')) return '6379';
  if (image.includes('mysql')) return '3306';
  if (image.includes('postgres')) return '5432';
  return '';
}

function parsePort(text?: string): number | null {
  const n = Number((text || '').trim());
  if (!Number.isInteger(n) || n <= 0 || n > 65535) return null;
  return n;
}
