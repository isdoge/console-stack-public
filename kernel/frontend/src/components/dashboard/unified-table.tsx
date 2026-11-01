import { displayTime } from '@/lib/display-time';
import { useEffect, useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, Eye, Filter, Plus, Search, Server, Settings2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Skeleton } from '@/components/ui/skeleton';
import { AccessBadges } from '@/components/service/access-badges';
import { EntryChain } from '@/components/service/entry-chain';
import { ReverseProxyRoutes } from '@/components/service/reverse-proxy-routes';
import { ServiceActions } from '@/components/service/service-actions';
import { VersionCell, versionsDiffer } from '@/components/service/version-cell';
import type { MonitorContainer, Service, ServiceHealth } from '@/lib/types';
import { cn } from '@/lib/utils';

export interface UnifiedRow {
  key: string;
  machine: string;
  name: string;
  purpose?: string;
  priority?: string;
  port?: number | string;
  image?: string;
  runStatus?: string;
  cpu?: number;
  memoryMb?: number;
  netIo?: number | string;
  service?: Service;
}

function normalize(name: string): string {
  return name.toLowerCase().replace(/[-_.\s]/g, '');
}

const PRIORITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };
const PRIORITY_LABEL: Record<string, string> = { high: '高', medium: '中', low: '低' };

function prioRank(p?: string): number {
  // 未登记容器（无 priority）排在低优先级之后
  return p != null ? (PRIORITY_RANK[p] ?? 1) : 3;
}

function machineLabel(name?: string): string {
  // Beszel 系统名（ALY/TXY）与台账机器名（aly/txy）统一小写，避免筛选重复
  return (name || '').toLowerCase();
}

/** 跨机器稳定身份：machine + docker + 容器资源名（优先 container 字段） */
export function identityKey(machine?: string, resource?: string): string {
  return `${machineLabel(machine)}:docker:${normalize(resource || '')}`;
}

export function isStatusUp(status?: string): boolean {
  const s = (status || '').toLowerCase();
  return s.includes('run') || s.includes('up');
}

export function isOutdated(svc?: Service): boolean {
  return versionsDiffer(
    svc?.version_info?.local,
    svc?.version_info?.upstream,
  );
}

/**
 * T3（§4.4）：证书剩余天数展示。只接受「探活成功 + 有限数字」的可信值；
 * `ssl_ok` 非 true、`ssl_days` 缺失/非有限数都回 null——`msg` 里的失败文案绝不转数字。
 * 色档：>30 正常，8–30 warning，<=7 destructive。
 */
export function certDisplay(
  health?: ServiceHealth
): { text: string; tone: 'ok' | 'warn' | 'bad' } | null {
  if (!health || health.ssl_ok !== true) return null;
  const days = health.ssl_days;
  if (typeof days !== 'number' || !Number.isFinite(days)) return null;
  const text = `${Math.round(days)} 天`;
  if (days <= 7) return { text, tone: 'bad' };
  if (days <= 30) return { text, tone: 'warn' };
  return { text, tone: 'ok' };
}

export function mergeRows(services: Service[], containers: MonitorContainer[]): UnifiedRow[] {
  const containerByKey = new Map<string, MonitorContainer>();
  containers.forEach((c) => {
    const machine = c.machine || c.system_name;
    const k = identityKey(machine, c.name);
    if (!containerByKey.has(k)) containerByKey.set(k, c);
  });

  const usedContainers = new Set<string>();
  const serviceRows: UnifiedRow[] = services.map((svc) => {
    const resource = svc.container || svc.name;
    const k = identityKey(svc.machine, resource);
    const c = containerByKey.get(k);
    if (c) usedContainers.add(k);
    return {
      key: `svc-${svc.machine}-${svc.name}`,
      machine: machineLabel(c?.machine || c?.system_name || svc.machine),
      name: svc.name,
      purpose: svc.purpose,
      priority: svc.priority || 'medium',
      port: svc.port,
      image: c?.image,
      runStatus: c?.status || svc.running,
      cpu: c?.cpu,
      memoryMb: c?.memory_mb,
      netIo: c?.net_io,
      service: svc,
    };
  });

  const containerRows: UnifiedRow[] = containers
    .filter((c) => !usedContainers.has(identityKey(c.machine || c.system_name, c.name)))
    .map((c) => ({
      key: `ctr-${c.system_id}-${c.name}`,
      machine: machineLabel(c.machine || c.system_name) || '-',
      name: c.name,
      image: c.image,
      runStatus: c.status,
      cpu: c.cpu,
      memoryMb: c.memory_mb,
      netIo: c.net_io,
    }));

  serviceRows.sort(
    (a, b) =>
      prioRank(a.priority) - prioRank(b.priority) ||
      a.machine.localeCompare(b.machine) ||
      a.name.localeCompare(b.name)
  );
  containerRows.sort((a, b) => a.machine.localeCompare(b.machine) || a.name.localeCompare(b.name));
  return [...serviceRows, ...containerRows];
}

interface ColumnDef {
  id: string;
  label: string;
  headerLabel?: string; // 表头展示文案，缺省用 label（菜单里始终用 label）
  headerTitle?: string; // 表头 hover 提示
  always?: boolean;
  sortable?: boolean;
}

const COLUMNS: ColumnDef[] = [
  { id: 'machine', label: '主机', sortable: true },
  { id: 'name', label: '名称', always: true, sortable: true },
  { id: 'purpose', label: '用途', sortable: true },
  { id: 'priority', label: '优先级', sortable: true },
  { id: 'port', label: '端口', sortable: true },
  { id: 'access', label: '访问', headerLabel: 'H｜T｜I', headerTitle: 'H = 家庭内网 / T = Tailscale / I = 公网；✅ 可达、❌ 不可达、? 未探测、- 未配置' },
  // C5 → L4：入口维度。与「访问」列分工——那列答「通不通」，这列答「谁在前面」
  { id: 'entry', label: '入口', headerTitle: '入口链路：域名 › 边缘 › tailnet › 内网（台账 + Caddy upstream 名单推导，零探测）' },
  { id: 'version', label: '版本' },
  { id: 'cert', label: '证书' },
  { id: 'image', label: '镜像', sortable: true },
  { id: 'status', label: '状态', sortable: true },
  { id: 'cpu', label: 'CPU', sortable: true },
  { id: 'memory', label: '内存(MB)', sortable: true },
  { id: 'net', label: '网络IO', sortable: true },
  { id: 'actions', label: '操作', always: true },
];

const HIDDEN_KEY = 'console.dashboard.hidden-columns';
const HIDDEN_PRIORITY_KEY = 'console.dashboard.hidden-priorities';
const HIDDEN_MACHINE_KEY = 'console.dashboard.hidden-machines';
const PAGE_SIZE_KEY = 'console.dashboard.page-size';
const DEFAULT_HIDDEN = ['image', 'net'];
const PAGE_SIZES = [10, 20, 50];
function loadPageSize(): number {
  try {
    const v = Number(localStorage.getItem(PAGE_SIZE_KEY));
    return PAGE_SIZES.includes(v) ? v : 10;
  } catch {
    return 10;
  }
}

function loadList(key: string, fallback: string[]): string[] {
  try {
    const raw = localStorage.getItem(key);
    if (raw == null) return fallback;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : fallback;
  } catch {
    return fallback;
  }
}

// A3：隐私模式/配额异常时 setItem 会抛异常；四处持久化 key 共用同一个静默降级包装，
// 异常时保留内存态即可，不阻断表格渲染或后续交互。
function safeSetItem(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // 存不进就算了，本次会话内的筛选/分页照常在内存里生效
  }
}

type SortState = { col: string; dir: 'asc' | 'desc' } | null;

function sortValue(row: UnifiedRow, col: string): string | number | undefined {
  switch (col) {
    case 'machine':
      return row.machine;
    case 'name':
      return row.name;
    case 'purpose':
      return row.purpose;
    case 'priority':
      return prioRank(row.priority);
    case 'port':
      return row.port != null ? Number(row.port) : undefined;
    case 'image':
      return row.image;
    case 'status':
      return row.runStatus;
    case 'cpu':
      return row.cpu;
    case 'memory':
      return row.memoryMb;
    case 'net':
      return typeof row.netIo === 'string' ? Number(row.netIo) || row.netIo : row.netIo;
    default:
      return undefined;
  }
}

function compareRows(a: UnifiedRow, b: UnifiedRow, sort: NonNullable<SortState>): number {
  const va = sortValue(a, sort.col);
  const vb = sortValue(b, sort.col);
  // 空值始终沉底，不随升降序翻转
  if (va == null && vb == null) return 0;
  if (va == null) return 1;
  if (vb == null) return -1;
  let cmp: number;
  if (typeof va === 'number' && typeof vb === 'number') {
    cmp = va - vb;
  } else {
    cmp = String(va).localeCompare(String(vb));
  }
  return sort.dir === 'asc' ? cmp : -cmp;
}

function runBadge(status?: string) {
  const s = (status || '').toLowerCase();
  // Beszel 原始状态是 "Up 2 days" 之类，统一归一为 running/stopped
  const up = isStatusUp(status);
  const stopped = s.includes('exit') || s.includes('stop') || s.includes('dead') || s.includes('created');
  const label = up ? 'running' : stopped ? 'stopped' : status || 'unknown';
  return (
    <Badge variant={up ? 'success' : 'secondary'} className="whitespace-nowrap">
      {label}
    </Badge>
  );
}

/** 健康时序迷你色带格数（与后端 service_health_history.BAND_SIZE 对齐）。 */
const MINI_BAND_SIZE = 12;

/**
 * T12 分支实验：服务健康迷你色带（状态格内）。
 * 只表达「最近 N 次健康检查的成败」，不是可用率图；无样本不渲染。
 */
export function HealthMiniBand({
  samples,
}: {
  samples?: Array<{ ts: string; status: string }>;
}) {
  const recent = (samples || []).slice(-MINI_BAND_SIZE);
  if (recent.length === 0) return null;
  const toneOf = (status: string) =>
    status === 'Healthy'
      ? 'bg-signal-1'
      : status === 'Unhealthy'
        ? 'bg-signal-5'
        : 'bg-muted-foreground/30';
  const labelOf = (status: string) =>
    status === 'Healthy' ? '健康' : status === 'Unhealthy' ? '故障' : status;
  return (
    <div
      className="flex items-center gap-[1.5px]"
      role="img"
      aria-label={`最近 ${recent.length} 次健康检查`}
      title="最近健康检查：绿=健康，红=故障"
    >
      {recent.map((s, i) => (
        <span
          key={`${s.ts}-${i}`}
          title={`${probeTimeLabel(s.ts)} · ${labelOf(s.status)}`}
          className={cn('h-3 w-1.5 rounded-[1.5px]', toneOf(s.status))}
        />
      ))}
    </div>
  );
}

/** 健康色带 tooltip 同时保留本地时间和原始采样时区。 */
function probeTimeLabel(ts: string): string {
  return displayTime(ts).title;
}

interface Props {
  services: Service[];
  containers: MonitorContainer[];
  loading: boolean;
  subtitle?: React.ReactNode;
  /** 单机视图的当前机器：本机行不受「隐藏服务器」过滤器影响（跨视图共享的 localStorage 不应藏掉本机服务） */
  activeMachine?: string;
  actionDisabled?: boolean;
  statFilter?: 'running' | 'healthy' | 'outdated' | null;
  onAdd: () => void;
  onSettings: (svc: Service) => void;
  onAction: (svc: Service, action: 'start' | 'stop' | 'restart' | 'logs') => void;
  onVersion: (svc: Service) => void;
  onContext?: (svc: Service) => void;
  onBatchPriority: (names: string[], priority: string) => Promise<void>;
}

export function UnifiedTable({
  services,
  containers,
  loading,
  subtitle,
  activeMachine,
  actionDisabled,
  statFilter,
  onAdd,
  onSettings,
  onAction,
  onVersion,
  onContext,
  onBatchPriority,
}: Props) {
  const [hidden, setHidden] = useState<string[]>(() => loadList(HIDDEN_KEY, DEFAULT_HIDDEN));
  const [hiddenPriorities, setHiddenPriorities] = useState<string[]>(() =>
    loadList(HIDDEN_PRIORITY_KEY, [])
  );
  const [hiddenMachines, setHiddenMachines] = useState<string[]>(() =>
    loadList(HIDDEN_MACHINE_KEY, [])
  );
  const [sort, setSort] = useState<SortState>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [batchSaving, setBatchSaving] = useState(false);
  const [pageSize, setPageSize] = useState<number>(loadPageSize);
  const [page, setPage] = useState(1);
  // T4：关键字过滤（名称/用途/镜像/主机），纯前端、跨页生效
  const [query, setQuery] = useState('');

  useEffect(() => {
    safeSetItem(HIDDEN_KEY, JSON.stringify(hidden));
  }, [hidden]);
  useEffect(() => {
    safeSetItem(HIDDEN_PRIORITY_KEY, JSON.stringify(hiddenPriorities));
  }, [hiddenPriorities]);
  useEffect(() => {
    safeSetItem(HIDDEN_MACHINE_KEY, JSON.stringify(hiddenMachines));
  }, [hiddenMachines]);

  const allRows = useMemo(() => mergeRows(services, containers), [services, containers]);
  const machines = useMemo(
    () => Array.from(new Set(allRows.map((r) => r.machine).filter((m) => m && m !== '-'))).sort(),
    [allRows]
  );
  const rows = useMemo(() => {
    let out = allRows.filter((r) => {
      // 未登记容器归入「未登记」分组，可单独隐藏
      const prioKey = r.service ? r.priority || 'medium' : 'unregistered';
      if (hiddenPriorities.includes(prioKey)) return false;
      if (hiddenMachines.includes(r.machine) && r.machine !== (activeMachine || "")) return false;
      // 概览卡片联动筛选：运行中/健康/待更新，口径与卡片一致只算已登记服务
      if (statFilter && !r.service) return false;
      if (statFilter === 'running' && !isStatusUp(r.runStatus)) return false;
      if (statFilter === 'healthy' && r.service?.health?.status !== 'Healthy') return false;
      if (statFilter === 'outdated' && !isOutdated(r.service)) return false;
      return true;
    });
    const q = query.trim().toLowerCase();
    if (q) {
      out = out.filter((r) =>
        [r.name, r.purpose, r.image, r.machine, r.port != null ? String(r.port) : '']
          .some((v) => (v || '').toLowerCase().includes(q))
      );
    }
    if (sort) out = [...out].sort((a, b) => compareRows(a, b, sort));
    return out;
  }, [allRows, hiddenPriorities, hiddenMachines, sort, statFilter, query, activeMachine]);

  // 前端分页：筛选/排序后再切页，超出范围时回退到末页
  const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
  const safePage = Math.min(page, totalPages);
  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);
  useEffect(() => {
    safeSetItem(PAGE_SIZE_KEY, String(pageSize));
  }, [pageSize]);
  const pagedRows = useMemo(
    () => rows.slice((safePage - 1) * pageSize, safePage * pageSize),
    [rows, safePage, pageSize]
  );
  const changePageSize = (size: number) => {
    setPageSize(size);
    setPage(1);
  };
  const visible = COLUMNS.filter((c) => c.always || !hidden.includes(c.id));

  const toggle = (id: string, show: boolean) => {
    setHidden((prev) => (show ? prev.filter((x) => x !== id) : [...prev, id]));
  };
  const toggleIn = (setter: React.Dispatch<React.SetStateAction<string[]>>) =>
    (id: string, show: boolean) => {
      setter((prev) => (show ? prev.filter((x) => x !== id) : [...prev, id]));
    };
  const togglePriority = toggleIn(setHiddenPriorities);
  const toggleMachine = toggleIn(setHiddenMachines);

  const handleSort = (col: string) => {
    setSort((prev) => {
      if (prev?.col !== col) return { col, dir: 'asc' };
      if (prev.dir === 'asc') return { col, dir: 'desc' };
      return null; // 第三次点击恢复默认排序（优先级 → 主机 → 名称）
    });
  };

  // 多选仅限当前页的已登记服务（未登记容器无台账可写）
  const selectableNames = useMemo(
    () => pagedRows.filter((r) => r.service).map((r) => r.name),
    [pagedRows]
  );
  const allSelected = selectableNames.length > 0 && selectableNames.every((n) => selected.includes(n));
  const toggleRow = (name: string, checked: boolean) => {
    setSelected((prev) => (checked ? [...prev, name] : prev.filter((n) => n !== name)));
  };
  const toggleAll = (checked: boolean) => {
    setSelected(checked ? selectableNames : []);
  };
  const applyBatch = async (priority: string) => {
    setBatchSaving(true);
    try {
      await onBatchPriority(selected, priority);
      setSelected([]);
    } catch {
      // 失败时保留选择，错误提示由上层 toast
    } finally {
      setBatchSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader className="flex-col items-stretch gap-3 space-y-0 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <CardTitle className="text-base">服务列表</CardTitle>
          {subtitle && <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>}
        </div>
        <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center sm:justify-end">
          {selected.length > 0 && (
            <div className="col-span-2 grid grid-cols-4 gap-1.5 rounded-md border bg-muted/40 px-2 py-1 sm:col-auto sm:flex sm:items-center">
              <span className="col-span-4 text-xs text-muted-foreground sm:col-auto">已选 {selected.length} · 设为</span>
              {([
                ['high', '高'],
                ['medium', '中'],
                ['low', '低'],
              ] as const).map(([value, label]) => (
                <Button
                  key={value}
                  size="sm"
                  variant="outline"
                  className="h-[30px] w-full px-2 text-xs sm:h-6 sm:w-auto"
                  disabled={batchSaving}
                  onClick={() => applyBatch(value)}
                >
                  {label}
                </Button>
              ))}
              <Button
                size="sm"
                variant="ghost"
                className="h-[30px] w-full px-2 text-xs sm:h-6 sm:w-auto"
                disabled={batchSaving}
                onClick={() => setSelected([])}
              >
                取消
              </Button>
            </div>
          )}
          <div className="relative col-span-2 sm:col-auto">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(1);
              }}
              placeholder="搜索名称/用途/镜像"
              className="h-[30px] w-full rounded-md border bg-background pl-7 pr-2 text-sm outline-none focus-visible:ring-1 focus-visible:ring-ring sm:h-8 sm:w-44"
            />
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="h-[30px] w-full sm:h-8 sm:w-auto">
                <Settings2 className="mr-2 h-4 w-4" />
                视图
              </Button>
            </DropdownMenuTrigger>
            {/* 高度不再写死：交给原语的 --radix-...-available-height，避免底部溢出视口够不着 */}
            <DropdownMenuContent align="end" className="w-[calc(100vw-1rem)] max-w-[362px] p-0 sm:w-auto sm:max-w-none">
              {/* Beszel 风格：各设置组分列平铺，不堆叠在同一列 */}
              <div className="grid grid-cols-4 divide-x sm:flex">
                <div className="min-w-0 py-1 sm:min-w-32">
                  <DropdownMenuLabel className="flex items-center gap-1.5">
                    <ArrowUpDown className="h-3.5 w-3.5" />
                    排序依据
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {COLUMNS.filter((c) => c.sortable).map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none hover:bg-accent hover:text-accent-foreground"
                      onClick={() => handleSort(c.id)}
                    >
                      <span className="flex w-4 justify-center">
                        {sort?.col === c.id &&
                          (sort.dir === 'asc' ? (
                            <ArrowUp className="h-3.5 w-3.5" />
                          ) : (
                            <ArrowDown className="h-3.5 w-3.5" />
                          ))}
                      </span>
                      {c.label}
                    </button>
                  ))}
                </div>
                <div className="min-w-0 py-1 sm:min-w-28">
                  <DropdownMenuLabel className="flex items-center gap-1.5">
                    <Filter className="h-3.5 w-3.5" />
                    优先级
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {([
                    ['high', '高'],
                    ['medium', '中'],
                    ['low', '低'],
                    ['unregistered', '未登记'],
                  ] as const).map(([value, label]) => (
                    <DropdownMenuCheckboxItem
                      key={value}
                      className="min-w-0 truncate pl-5 pr-1 [&>span]:left-1 sm:pl-8 sm:pr-2 sm:[&>span]:left-2"
                      checked={!hiddenPriorities.includes(value)}
                      onCheckedChange={(checked) => togglePriority(value, !!checked)}
                      onSelect={(e) => e.preventDefault()}
                    >
                      {label}
                    </DropdownMenuCheckboxItem>
                  ))}
                </div>
                <div className="min-w-0 py-1 sm:min-w-28">
                  <DropdownMenuLabel className="flex items-center gap-1.5">
                    <Server className="h-3.5 w-3.5" />
                    服务器
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {machines.map((m) =>
                    activeMachine && m === activeMachine ? (
                      <DropdownMenuCheckboxItem
                        key={m}
                        className="min-w-0 truncate pl-5 pr-1 [&>span]:left-1 sm:pl-8 sm:pr-2 sm:[&>span]:left-2"
                        checked
                        disabled
                        title="单机视图始终显示本机服务"
                      >
                        {m}
                      </DropdownMenuCheckboxItem>
                    ) : (
                      <DropdownMenuCheckboxItem
                        key={m}
                        className="min-w-0 truncate pl-5 pr-1 [&>span]:left-1 sm:pl-8 sm:pr-2 sm:[&>span]:left-2"
                        checked={!hiddenMachines.includes(m)}
                        onCheckedChange={(checked) => toggleMachine(m, !!checked)}
                        onSelect={(e) => e.preventDefault()}
                      >
                        {m}
                      </DropdownMenuCheckboxItem>
                    )
                  )}
                </div>
                <div className="min-w-0 py-1 sm:min-w-32">
                  <DropdownMenuLabel className="flex items-center gap-1.5">
                    <Eye className="h-3.5 w-3.5" />
                    可见列
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {COLUMNS.filter((c) => !c.always).map((c) => (
                      <DropdownMenuCheckboxItem
                        key={c.id}
                        className="min-w-0 truncate pl-5 pr-1 [&>span]:left-1 sm:pl-8 sm:pr-2 sm:[&>span]:left-2"
                        checked={!hidden.includes(c.id)}
                      onCheckedChange={(checked) => toggle(c.id, !!checked)}
                      onSelect={(e) => e.preventDefault()}
                    >
                      {c.label}
                    </DropdownMenuCheckboxItem>
                  ))}
                </div>
              </div>
            </DropdownMenuContent>
          </DropdownMenu>
          <Button size="sm" className="h-[30px] w-full sm:h-8 sm:w-auto" onClick={onAdd}>
            <Plus className="mr-2 h-4 w-4" />
            添加服务
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {loading ? (
          <div className="space-y-2 p-6">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <p className="p-6 text-center text-sm text-muted-foreground">暂无服务数据</p>
        ) : (
          <div className="overflow-x-auto" role="region" aria-label="服务列表滚动区域" tabIndex={0}>
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/30 text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="w-8 px-3 py-2">
                    <input
                      type="checkbox"
                      className="h-3.5 w-3.5 accent-primary"
                      checked={allSelected}
                      onChange={(e) => toggleAll(e.target.checked)}
                      title="全选本页已登记服务"
                    />
                  </th>
                  {visible.map((c) => {
                    const alignRight = ['cpu', 'memory', 'net', 'actions'].includes(c.id);
                    return (
                      <th key={c.id} title={c.headerTitle} className={`${alignRight ? 'text-right' : 'text-left'} whitespace-nowrap px-4 py-2`}>
                        {c.sortable ? (
                          <button
                            type="button"
                            className={`inline-flex items-center gap-1 uppercase hover:text-foreground ${
                              sort?.col === c.id ? 'text-foreground' : ''
                            }`}
                            onClick={() => handleSort(c.id)}
                          >
                            {c.headerLabel ?? c.label}
                            {sort?.col === c.id &&
                              (sort.dir === 'asc' ? (
                                <ArrowUp className="h-3 w-3" />
                              ) : (
                                <ArrowDown className="h-3 w-3" />
                              ))}
                          </button>
                        ) : (
                          c.headerLabel ?? c.label
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {pagedRows.map((row) => (
                  <tr key={row.key} className="border-b last:border-0 hover:bg-muted/30">
                    <td className="w-8 px-3 py-2">
                      {row.service && (
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 accent-primary"
                          checked={selected.includes(row.name)}
                          onChange={(e) => toggleRow(row.name, e.target.checked)}
                        />
                      )}
                    </td>
                    {visible.map((c) => (
                      <RowCell key={c.id} col={c.id} row={row} {...{ actionDisabled, onSettings, onAction, onVersion, onContext }} />
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!loading && rows.length > 0 && (
          <div className="flex flex-col gap-2 border-t px-4 py-2 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center justify-between gap-2 sm:justify-start">
              <span>共 {rows.length} 条 · 每页</span>
              {PAGE_SIZES.map((size) => (
                <Button
                  key={size}
                  size="sm"
                  variant={pageSize === size ? 'secondary' : 'ghost'}
                  className="h-[30px] min-w-[30px] px-2 text-xs sm:h-6 sm:min-w-0"
                  onClick={() => changePageSize(size)}
                >
                  {size}
                </Button>
              ))}
            </div>
            <div className="flex items-center justify-between gap-1 sm:justify-start">
              <Button
                size="sm"
                variant="ghost"
                className="h-[30px] w-[30px] p-0 sm:h-6 sm:w-auto sm:px-1.5"
                disabled={safePage <= 1}
                onClick={() => setPage(safePage - 1)}
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </Button>
              <span>
                第 {safePage} / {totalPages} 页
              </span>
              <Button
                size="sm"
                variant="ghost"
                className="h-[30px] w-[30px] p-0 sm:h-6 sm:w-auto sm:px-1.5"
                disabled={safePage >= totalPages}
                onClick={() => setPage(safePage + 1)}
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

interface CellProps {
  col: string;
  row: UnifiedRow;
  actionDisabled?: boolean;
  onSettings: (svc: Service) => void;
  onAction: (svc: Service, action: 'start' | 'stop' | 'restart' | 'logs') => void;
  onVersion: (svc: Service) => void;
  onContext?: (svc: Service) => void;
}

function RowCell({ col, row, actionDisabled, onSettings, onAction, onVersion, onContext }: CellProps) {
  switch (col) {
    case 'machine':
      return <td className="whitespace-nowrap px-4 py-2 text-xs">{row.machine || '-'}</td>;
    case 'name':
      return (
        <td className="w-[168px] min-w-[168px] max-w-[168px] px-4 py-2 font-medium">
          <div className="scrollbar-hidden w-full overflow-x-auto whitespace-nowrap overscroll-x-contain" role="region" aria-label={`${row.name} 名称，左右滑动查看`} title={row.name} tabIndex={0}>
            {row.service && onContext ? (
              <button type="button" className="min-h-[30px] min-w-[30px] rounded px-2 text-left underline decoration-dotted underline-offset-4 hover:text-primary focus-visible:outline focus-visible:outline-ring md:min-h-0 md:px-0"
                aria-label={`查看 ${row.name} 服务上下文`} onClick={() => onContext(row.service!)}>{row.name}</button>
            ) : row.name}
            {!row.service && (
              <span className="ml-2 text-xs font-normal text-muted-foreground">未登记</span>
            )}
          </div>
        </td>
      );
    case 'purpose':
      return (
        <td className="min-w-72 max-w-[32rem] px-4 py-2 align-top text-xs">
          <div className="break-words leading-5 text-muted-foreground" title={row.purpose}>
            {row.purpose || '-'}
          </div>
          {row.service &&
            (Boolean(row.service.proxy_routes?.length) || Boolean(row.service.proxy_error)) && (
              <div className="mt-1.5 border-t border-border/60 pt-1.5">
                <ReverseProxyRoutes svc={row.service} />
              </div>
            )}
        </td>
      );
    case 'priority': {
      if (!row.service) return <td className="px-4 py-2 text-xs text-muted-foreground">-</td>;
      const p = row.priority || 'medium';
      const variant = p === 'high' ? 'default' : p === 'low' ? 'outline' : 'secondary';
      return (
        <td className="px-4 py-2">
          <Badge variant={variant}>{PRIORITY_LABEL[p] || p}</Badge>
        </td>
      );
    }
    case 'port':
      return <td className="px-4 py-2 font-mono text-xs">{row.port ?? '-'}</td>;
    case 'access':
      return (
        <td className="px-4 py-2">
          {row.service ? <AccessBadges svc={row.service} /> : <span className="text-xs text-muted-foreground">-</span>}
        </td>
      );
    case 'entry':
      return (
        <td className="px-4 py-2">
          {row.service ? (
            <EntryChain entry={row.service.entry} />
          ) : (
            <span className="text-xs text-muted-foreground">-</span>
          )}
        </td>
      );
    case 'version':
      return (
        <td className="px-4 py-2">
          {row.service ? <VersionCell svc={row.service} onOpen={onVersion} /> : <span className="text-xs text-muted-foreground">-</span>}
        </td>
      );
    case 'cert': {
      const cert = row.service ? certDisplay(row.service.health) : null;
      if (!cert) return <td className="px-4 py-2 text-xs text-muted-foreground">—</td>;
      const toneCls = cert.tone === 'bad' ? 'text-destructive' : cert.tone === 'warn' ? 'text-amber-600' : 'text-foreground';
      return <td className={`px-4 py-2 font-mono text-xs ${toneCls}`}>{cert.text}</td>;
    }
    case 'image':
      return (
        <td className="max-w-56 truncate px-4 py-2 font-mono text-xs" title={row.image}>
          {row.image || '-'}
        </td>
      );
    case 'status':
      return (
        <td className="px-4 py-2">
          <div className="flex items-center gap-2">
            {runBadge(row.runStatus)}
            <HealthMiniBand samples={row.service?.health_history} />
          </div>
        </td>
      );
    case 'cpu':
      return <td className="px-4 py-2 text-right font-mono">{row.cpu != null ? `${row.cpu}%` : '-'}</td>;
    case 'memory':
      return <td className="px-4 py-2 text-right font-mono">{row.memoryMb ?? '-'}</td>;
    case 'net':
      return <td className="px-4 py-2 text-right font-mono">{row.netIo ?? '-'}</td>;
    case 'actions':
      return (
        <td className="px-4 py-2 text-right">
          {row.service ? (
            <ServiceActions
              svc={row.service}
              disabled={actionDisabled}
              onSettings={onSettings}
              onAction={onAction}
            />
          ) : null}
        </td>
      );
    default:
      return <td className="px-4 py-2">-</td>;
  }
}
