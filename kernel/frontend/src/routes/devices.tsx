import { NavigationLink } from '@/components/navigation-link';
import { usePluginNavigation } from '@/lib/plugins/navigation';
import type { NavigationContribution } from '@/lib/plugins/navigation-types';
import { DeviceServiceContext } from '@/components/devices/device-service-context';
import { DeviceHistoryPanel } from '@/components/devices/device-history-panel';
import { DisplayTime } from '@/components/ui/display-time';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { ScanSearch, SlidersHorizontal } from 'lucide-react';
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
import { MachineCard } from '@/components/monitor/machine-card';
import { MachineCompare } from '@/components/monitor/machine-compare';
import { clearDeviceActionsController, setDeviceActionsController } from '@/components/monitor/device-actions';
import { MachineSheet } from '@/components/monitor/machine-sheet';
import { MachineOverviewCard } from '@/components/dashboard/machine-overview-card';
import type {
  MachineOverviewSnapshot,
  MetricCapability,
} from '@/components/dashboard/machine-overview-card';
import { ObservabilityMachine } from '@/components/monitor/observability-machine';
import { DiscoverDialog } from '@/components/service/discover-dialog';
import {
  buildRoleMap,
  countOfflineServers,
  countServicesByMachine,
  filterContainersByMachine,
  filterServicesByMachine,
  machineIdOf,
} from '@/components/monitor/partition';
import { UnifiedTable, isOutdated, isStatusUp, mergeRows } from '@/components/dashboard/unified-table';
import { LogsDialog } from '@/components/service/logs-dialog';
import { ReverseProxyRoutes } from '@/components/service/reverse-proxy-routes';
import { ServiceSheet } from '@/components/service/service-sheet';
import { useEventCallback } from '@/lib/use-event-callback';
import { StatCards } from '@/components/service/stat-cards';
import type { StatFilter } from '@/components/service/stat-cards';
import { VersionDialog } from '@/components/service/version-dialog';
import {
  getCapacity,
  getConfig,
  getMonitor,
  getObservability,
  getServicesSnapshot,
  listMachines,
  runServiceAction,
  setServicesPriority,
  apiErrorMessage,
} from '@/lib/api';
import type {
  CapacityPayload,
  DiskTrend,
  Machine,
  MonitorContainer,
  MonitorPayload,
  MonitorSystem,
  ObservabilityPayload,
  Service,
  ServiceAction,
  SnapshotMeta,
} from '@/lib/types';
import { cn } from '@/lib/utils';

const FILTERS: StatFilter[] = ['running', 'healthy', 'outdated'];

// ---- B3-1：单机页四分区 + 按能力裁剪 ----

/**
 * B1-1 §4.4 能力键 → 分区/模块的裁剪判定。
 * 返回 null = 该能力键无声明，回落数据驱动（现状行为：有数据才显示）；
 * 'supported' / 'partial' 都算可显示（partial 带 note 限定子字段，仍值得看）。
 */
function resolveCapability(
  snapshot: MachineOverviewSnapshot | null | undefined,
  ...keys: string[]
): CapabilityDecision {
  const caps = snapshot?.capabilities;
  if (!caps) return null;
  for (const key of keys) {
    const cap: MetricCapability | undefined = caps[key];
    if (cap) return cap.status === 'unsupported' ? 'unsupported' : 'supported';
  }
  return null;
}

type CapabilityDecision = 'supported' | 'unsupported' | null;

/**
 * 单机页能力裁剪（B1-1 §4.4）：
 * 1. 契约 `snapshot.capabilities`（B1 权威）优先；
 * 2. 台账 `machine.capabilities`（console/machines.yaml，现状事实源）兜底——
 *    kr/sg 显式声明只有 beszel+ssh（无 docker）→ 容器数据和扫描入口裁剪；
 * 3. 都没声明 → 回落「有数据才显示」（现状行为）。**只有明确声明不支持才裁剪**，
 *    「未声明」不等于「不支持」（B1-1 §4.4 规则 3：能力基于平台事实，不基于采没采到）。
 */
function deviceCapabilityGate(
  machine: Machine,
  snapshot: MachineOverviewSnapshot | null | undefined,
  key: 'containers' | 'systemd' | 'smart'
): boolean {
  const declared = resolveCapability(snapshot, key);
  if (declared) return declared === 'supported';
  // 台账兜底：显式声明了能力列表但不含 docker → 无容器能力；未声明列表 → 回落数据驱动
  if (key === 'containers') {
    const caps = machine.capabilities;
    if (!caps) return true;
    return caps.includes('docker');
  }
  // systemd 白名单（us/kr/sg）与 SMART（仅 nas）无台账能力位：白名单空=未配置，
  // 回落数据驱动（有 systemd_services/smart_devices 行才显示，现状行为）
  return true;
}

// ---- T7：单机视图各块按需显隐（localStorage 持久，跨机器共用一套口径） ----

interface BlockPrefs {
  events: boolean;
  systemd: boolean;
  storage: boolean;
  probeHistory: boolean;
  traffic: boolean;
  diskTrend: boolean;
  caddy: boolean;
  navigation: boolean;
}

const DEFAULT_BLOCKS: BlockPrefs = {
  events: true,
  systemd: true,
  storage: true,
  probeHistory: true,
  traffic: true,
  diskTrend: true,
  caddy: true,
  navigation: true,
};

const BLOCK_LABELS: Record<keyof BlockPrefs, string> = {
  events: '本机事件',
  systemd: 'systemd 服务',
  storage: '存储设备',
  probeHistory: '探测历史',
  traffic: '流量账本',
  diskTrend: '磁盘趋势',
  caddy: 'Caddy 路由',
  navigation: '关联入口',
};

const BLOCKS_KEY = 'console.device.blocks';

function loadBlocks(): BlockPrefs {
  try {
    const raw = localStorage.getItem(BLOCKS_KEY);
    if (!raw) return DEFAULT_BLOCKS;
    const stored = JSON.parse(raw) as Partial<BlockPrefs> & { panels?: boolean };
    // 旧面板入口偏好沿用到通用导航块，保留用户的隐藏选择。
    return { ...DEFAULT_BLOCKS, ...stored, navigation: stored.navigation ?? stored.panels ?? true };
  } catch {
    return DEFAULT_BLOCKS;
  }
}

/**
 * 角色可见的模块键：server / node 各自的块集合（菜单与渲染共用一份，防漂移）。
 * B3-1：探测历史迁入「可用性区」，server 也开放（`/api/machines` 对 server 也带
 * `probe_history`，backend app.py:538/572）；磁盘趋势仍 server 专属（后端只对 server 算）。
 */
const SERVER_BLOCKS: Array<keyof BlockPrefs> = [
  'events',
  'systemd',
  'storage',
  'probeHistory',
  'diskTrend',
  'caddy',
  'navigation',
];
const NODE_BLOCKS: Array<keyof BlockPrefs> = ['events', 'systemd', 'storage', 'probeHistory', 'traffic'];

function parseFilter(raw: string | null): StatFilter {
  if (raw && (FILTERS as string[]).includes(raw)) return raw as StatFilter;
  return null;
}

interface Props {
  refreshKey: number;
  refreshForce?: boolean;
  onLoaded?: () => void;
}

/**
 * B2：机器 tab 条（§5.2）。`/devices` = 全部，`/devices/:id` = 单机。
 * 计数口径：已登记服务数；node（kr/sg）没有 Docker 服务，显示「—」而非 0。
 */
function MachineTabs({
  machines,
  counts,
  activeId,
}: {
  machines: Machine[];
  counts: Record<string, number>;
  activeId: string | null;
}) {
  const tabClass = (active: boolean) =>
    cn(
      'inline-flex h-[30px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 text-sm transition-colors sm:h-auto sm:py-1.5',
      active
        ? 'border-primary bg-primary/10 text-primary'
        : 'border-border/60 bg-card/75 text-muted-foreground hover:border-primary/50'
    );

  return (
    <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:gap-1.5">
      <nav aria-label="选择设备" className="scrollbar-hidden flex w-full min-w-0 items-center gap-1.5 overflow-x-auto overscroll-x-contain sm:w-auto sm:flex-1">
        <Link to="/devices" className={tabClass(activeId == null)}>
          全部
        </Link>
        {machines.map((m) => (
          <Link key={m.id} to={`/devices/${m.id}`} className={tabClass(activeId === m.id)}>
            {m.label}
            <span className="rounded-full bg-muted px-1.5 py-0.5 font-mono text-xs tabular-nums">
              {m.role === 'node' ? '—' : counts[m.id] ?? 0}
            </span>
          </Link>
        ))}
      </nav>
    </div>
  );
}

/** 可用性区：探测历史明细（旧→新，最多 20 条）。server 与 node 都渲染（B3-1 迁入可用性分区）。 */
function ProbeHistoryDetail({ machine }: { machine: Machine }) {
  const samples = machine.probe_history || [];
  if (samples.length === 0) return null;
  return (
    <Card data-section="availability">
      <CardHeader>
        <CardTitle className="text-base">探测历史</CardTitle>
      </CardHeader>
      <CardContent>
        <table className="w-full text-sm">
          <thead className="border-b text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-2 py-1 text-left">时间</th>
              <th className="px-2 py-1 text-left">状态</th>
              <th className="px-2 py-1 text-left">耗时</th>
            </tr>
          </thead>
          <tbody>
            {samples.map((s, i) => (
              <tr key={`${s.ts}-${i}`} className="border-b last:border-0">
                <td className="px-2 py-1 font-mono text-xs">{s.ts}</td>
                <td className="px-2 py-1 text-xs">{s.status}</td>
                <td className="px-2 py-1 font-mono text-xs">
                  {s.latency_ms != null ? `${s.latency_ms} ms` : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

/** 概览区补充：B2-2 设备概况卡（B3 复用，不重复造卡）。B1 契约字段就绪即生效。 */
function OverviewCardSlot({
  machine,
  system,
  sourceFailed,
}: {
  machine: Machine;
  system?: MonitorSystem;
  sourceFailed: boolean;
}) {
  const snapshot: MachineOverviewSnapshot | null = system ? { ...system } : null;
  return (
    <MachineOverviewCard
      machine={machine}
      snapshot={snapshot}
      sourceFailed={sourceFailed}
      to={null}
    />
  );
}

/** node 单机视图：流量账本明细（E1 摘要逐字段摊开）。 */
function TrafficLedgerDetail({ machine }: { machine: Machine }) {
  const t = machine.traffic;
  if (!t) return null;
  const p = t.projection;
  const fields: Array<[string, string]> = [
    ['月份', t.month],
    ['计费网卡', t.nic || '自动'],
    ['本月已用（出站）', `${t.used_gb} GB`],
    ['配额', t.quota_gb ? `${t.quota_gb} GB` : '—'],
    ['已用占比', t.pct != null ? `${t.pct.toFixed(1)}%` : '—'],
    ['累计发送', `${t.sent_gb} GB`],
    ['累计接收', `${t.recv_gb} GB`],
    ['起算时间', t.since || '未知'],
    ['归零次数', String(t.resets ?? 0)],
    // M1：预测四格恒占位（不像 hover 那样有才显），让「为什么现在没有预测」
    // 本身也是可读的信息——观测不足 72 小时时后端回 null，这里显式写出来
    ['日均出站（M1）', p ? `${p.daily_gb} GB/天` : '观测不足 72 小时'],
    ['月底预计（M1）', p ? `${p.projected_gb} GB` : '—'],
    ['预计占配额（M1）', p && p.projected_pct != null ? `${p.projected_pct}%` : '—'],
    ['预测口径', p ? (p.partial_month ? '从首次采样起算，非整月' : '整月') : '—'],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">流量账本</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {fields.map(([label, value]) => (
            <div key={label} className="rounded-md bg-muted/40 px-2.5 py-2">
              <div className="text-xs text-muted-foreground">{label}</div>
              <div className="mt-0.5 truncate font-mono text-sm" title={typeof value === 'string' ? value : undefined}>
                {value}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

/** M2：趋势方向 → 中文 + 色调。`flat` 不上色——「基本不动」不是需要注意的状态。 */
export function trendTone(trend: string): { label: string; cls: string } {
  if (trend === 'rising') return { label: '在增长', cls: 'text-amber-700' };
  if (trend === 'falling') return { label: '在减少', cls: 'text-emerald-600' };
  return { label: '基本不动', cls: 'text-muted-foreground' };
}

/**
 * M2：`days_to_full` 为 null 的三种成因后端刻意不区分，UI 统一「暂不预测」。
 * 只在能**如实**判断时补一句为什么——即拿后端下发的 `min_r_squared` 比对，
 * 而不是在前端另抄一个阈值（两边各写一个 0.5 迟早会漂）。拿不到阈值就不解释。
 */
export function etaText(trend: DiskTrend, minRSquared: number | null): string {
  if (trend.days_to_full != null) return `约 ${trend.days_to_full} 天后写满`;
  if (minRSquared != null && trend.r_squared != null && trend.r_squared < minRSquared) {
    return '暂不预测（趋势不稳定）';
  }
  return '暂不预测';
}

/**
 * server 单机视图：磁盘增长趋势（M2）。
 *
 * **没有趋势就整块不渲染**：后端只对 role=server 且样本足够的机器给数据，
 * 给一张「暂无数据」的空卡只会占地方。取数失败（`ok=false`）同理——
 * 那是 Beszel 侧的问题，今日关注的 beszel 条目已经在报了。
 */
function DiskTrendCard({ machine, capacity }: { machine: Machine; capacity: CapacityPayload | null }) {
  const trend = capacity?.machines?.[machine.id];
  if (!trend) return null;
  const tone = trendTone(trend.trend);
  const fields: Array<[string, React.ReactNode, string?]> = [
    ['趋势', tone.label, tone.cls],
    ['日增', `${trend.daily_gb > 0 ? '+' : ''}${trend.daily_gb} GB/天`, tone.cls],
    [
      '预计耗尽',
      etaText(trend, capacity?.min_r_squared ?? null),
      trend.days_to_full != null ? 'text-amber-700' : undefined,
    ],
    ['当前已用', `${trend.disk_used_gb} / ${trend.disk_total_gb} GB`],
    ['剩余', `${trend.disk_free_gb} GB`],
    ['拟合优度 r²', trend.r_squared != null ? trend.r_squared.toFixed(3) : '—'],
    ['样本', `${trend.samples} 条 / ${(trend.span_hours / 24).toFixed(1)} 天`],
    ['取样区间', <><DisplayTime value={trend.first_at} /> → <DisplayTime value={trend.last_at} /></>],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">磁盘趋势</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {fields.map(([label, value, cls]) => (
            <div key={label} className="rounded-md bg-muted/40 px-2.5 py-2">
              <div className="text-xs text-muted-foreground">{label}</div>
              <div className={cn('mt-0.5 truncate font-mono text-sm', cls)} title={typeof value === 'string' ? value : undefined}>
                {value}
              </div>
            </div>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          按 {capacity?.source === 'console_pg' ? 'Console PG' : 'Beszel'} {capacity?.history_type || '480m'} 历史线性拟合，仅供参考；预测只作提示，
          不触发任何清理或扩容。
        </p>
      </CardContent>
    </Card>
  );
}

/** server 单机视图：本机服务的 Caddy 反代路由汇总。 */
function CaddyRoutesCard({ services }: { services: Service[] }) {
  const withRoutes = services.filter((s) => s.proxy_routes?.length || s.proxy_error);
  if (withRoutes.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Caddy 路由</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {withRoutes.map((svc) => (
          <div key={svc.name}>
            <div className="mb-1 text-xs font-medium text-muted-foreground">{svc.name}</div>
            <ReverseProxyRoutes svc={svc} />
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

/** 插件投影的同设备快捷入口。 */
function MachineNavigationCards({ machine, navigation }: { machine: Machine; navigation: NavigationContribution[] }) {
  return navigation.map((group) => {
    const links = group.links.filter((link) => link.machine?.toLowerCase() === machine.id.toLowerCase());
    if (!links.length) return null;
    return <Card key={group.owner}>
      <CardHeader><CardTitle className="text-base">{group.label}</CardTitle></CardHeader>
      <CardContent className="space-y-1">{links.map((link) => (
        <NavigationLink key={link.id} target={link} className="block truncate text-sm text-primary hover:underline">{link.label}</NavigationLink>
      ))}</CardContent>
    </Card>;
  });
}

interface MachineDetailProps {
  machine: Machine;
  system?: MonitorSystem;
  services: Service[];
  containers: MonitorContainer[];
  navigation: NavigationContribution[];
  loading: boolean;
  subtitle?: React.ReactNode;
  actionDisabled?: boolean;
  /** J（§4.2）：本机 Beszel 事件/systemd/SMART；失败为 null 时该区整体不渲染 */
  observability?: ObservabilityPayload | null;
  /** M2：磁盘增长趋势；未取到或本机样本不足时该卡不渲染 */
  capacity?: CapacityPayload | null;
  /** B3-1：快照级来源失败（monitor.error / snapshot.error），概况卡降级显示最后已知值 */
  monitorSourceFailed?: boolean;
  monitorSource?: string;
  refreshKey: number;
  onAdd: () => void;
  onSettings: (svc: Service) => void;
  onAction: (svc: Service, action: ServiceAction) => void;
  onVersion: (svc: Service) => void;
  onContext: (svc: Service) => void;
  onBatchPriority: (names: string[], priority: string) => Promise<void>;
  onActionDone: () => void;
  /** T6：只读扫描本机未登记容器（server 视图才显示入口） */
  onScan?: () => void;
  /** T7：各块显隐偏好与切换 */
  blocks?: BlockPrefs;
  onToggleBlock?: (key: keyof BlockPrefs, value: boolean) => void;
}

/** B2 单机视图（§5.2）：详情卡 + 本机服务/Caddy 路由/关联面板；node 换成动作/探测/流量账本。 */
function MachineDetail({
  machine,
  system,
  services,
  containers,
  navigation,
  loading,
  subtitle,
  actionDisabled,
  observability,
  capacity,
  monitorSourceFailed = false,
  monitorSource,
  refreshKey,
  onAdd,
  onSettings,
  onAction,
  onVersion,
  onContext,
  onBatchPriority,
  onActionDone,
  onScan,
  blocks = DEFAULT_BLOCKS,
  onToggleBlock,
}: MachineDetailProps) {
  const isNode = machine.role === 'node';
  const blockKeys = (isNode ? NODE_BLOCKS : SERVER_BLOCKS).filter((key) => key !== 'navigation' || navigation.length > 0);

  // B1-1 §4.4 能力仅裁剪监控数据；服务台账管理不依赖 Docker。
  const snapshot: MachineOverviewSnapshot | null = system ? { ...system } : null;
  const showSystemdBlock = deviceCapabilityGate(machine, snapshot, 'systemd');
  const showStorageBlock = deviceCapabilityGate(machine, snapshot, 'smart');
  const showContainers = deviceCapabilityGate(machine, snapshot, 'containers');

  return (
    <div className="space-y-6">
      {/* ---- 概览区（B3-1）：B2-2 设备概况卡 + 现有资产详情卡 ---- */}
      <section aria-label="概览" data-section="overview" className="space-y-6">
        {/* B2-2 概况卡（B3 复用，不重复造卡）：system 缺席时组件按 never-collected
            显式表达，不画假读数；sourceFailed 传入后降级展示最后已知值 */}
        <OverviewCardSlot machine={machine} system={system} sourceFailed={monitorSourceFailed} />
        {/* 详情卡统一走 MachineCard，且必须喂真 machine（台账字段齐全）：
            只拿 monitor 现合的最小 Machine 会走 §4.3 退化路径，价格/流量/色带全丢 */}
        <MachineCard machine={machine} system={system} onActionDone={onActionDone} />
      </section>

      {/* ---- 服务区：所有机器保留台账与添加入口；容器数据和扫描按能力显示 ---- */}
      <section aria-label="服务" data-section="services">
        <UnifiedTable
          services={services}
          containers={showContainers ? containers : []}
          loading={loading}
          subtitle={subtitle}
          activeMachine={machine.id}
          actionDisabled={actionDisabled}
          statFilter={null}
          onAdd={onAdd}
          onSettings={onSettings}
          onAction={onAction}
          onVersion={onVersion}
          onContext={onContext}
          onBatchPriority={onBatchPriority}
        />
        <div className="mt-2 flex items-center gap-2 sm:justify-end">
          {onToggleBlock && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm" className="h-[30px] flex-1 sm:h-8 sm:flex-none">
                  <SlidersHorizontal className="mr-1 h-3.5 w-3.5" />
                  显示模块
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuLabel>单机视图模块</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {blockKeys.map((key) => (
                  <DropdownMenuCheckboxItem
                    key={key}
                    checked={blocks[key]}
                    onCheckedChange={(v) => onToggleBlock(key, !!v)}
                    onSelect={(e) => e.preventDefault()}
                  >
                    {key === 'navigation' && navigation.length === 1 ? navigation[0].label : BLOCK_LABELS[key]}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          {showContainers && onScan && (
            <Button variant="outline" size="sm" className="h-[30px] flex-1 sm:h-8 sm:flex-none" onClick={onScan}>
              <ScanSearch className="mr-1 h-3.5 w-3.5" />
              扫描未登记容器
            </Button>
          )}
        </div>
        {blocks.diskTrend && <DiskTrendCard machine={machine} capacity={capacity ?? null} />}
        {blocks.caddy && <CaddyRoutesCard services={services} />}
        {blocks.navigation && <MachineNavigationCards machine={machine} navigation={navigation} />}
      </section>

      <DeviceHistoryPanel machine={machine} snapshot={system} source={monitorSource} refreshKey={refreshKey} />

      {/* ---- 可用性区：探测历史与指标历史分别展示 ---- */}
      {blocks.probeHistory && <ProbeHistoryDetail machine={machine} />}

      {/* ---- 事件区：本机事件/systemd/SMART；systemd/存储按 B1-1 §4.4 能力裁剪 ---- */}
      <section aria-label="事件" data-section="events">
        <ObservabilityMachine
          machine={machine}
          data={observability}
          visible={{
            events: blocks.events,
            systemd: blocks.systemd && showSystemdBlock,
            storage: blocks.storage && showStorageBlock,
          }}
        />
      </section>

      {isNode && blocks.traffic && <TrafficLedgerDetail machine={machine} />}
    </div>
  );
}

export function DevicesRoute({ refreshKey, refreshForce = true, onLoaded }: Props) {
  const { id } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [services, setServices] = useState<Service[]>([]);
  const [servicesSnapshot, setServicesSnapshot] = useState<SnapshotMeta | null>(null);
  const [monitor, setMonitor] = useState<MonitorPayload>({ systems: [], containers: [] });
  const [machines, setMachines] = useState<Machine[]>([]);
  const navigation = usePluginNavigation();
  const [observability, setObservability] = useState<ObservabilityPayload | null>(null);
  const [capacity, setCapacity] = useState<CapacityPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [machinesLoaded, setMachinesLoaded] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [interval, setIntervalSec] = useState(600);
  const previousRefreshKey = useRef(refreshKey);
  const previousObservabilityKey = useRef(refreshKey);
  const servicesRequest = useRef(0);
  const monitorRequest = useRef(0);
  const servicesPoll = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const monitorPoll = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const [statFilter, setStatFilter] = useState<StatFilter>(() => parseFilter(searchParams.get('filter')));
  const [contextService, setContextService] = useState<Service | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [sheetMode, setSheetMode] = useState<'edit' | 'add'>('add');
  const [sheetSvc, setSheetSvc] = useState<Service | null>(null);

  // B3：机器台账增删（与服务的 sheet 各一套 state，互不干扰）
  const [machineSheetOpen, setMachineSheetOpen] = useState(false);
  const [machineSheetMode, setMachineSheetMode] = useState<'edit' | 'add'>('add');
  const [machineSheetId, setMachineSheetId] = useState<string | null>(null);

  const [logsOpen, setLogsOpen] = useState(false);
  const [logs, setLogs] = useState('');

  // T6：扫描未登记容器对话框（server 单机视图）
  const [discoverOpen, setDiscoverOpen] = useState(false);
  // M3：机器临时对比（选择不持久化，见 MachineCompare 注释）
  const [compareOpen, setCompareOpen] = useState(false);
  // T7：单机视图模块显隐偏好（localStorage 持久）
  const [blocks, setBlocks] = useState<BlockPrefs>(loadBlocks);

  useEffect(() => {
    try {
      localStorage.setItem(BLOCKS_KEY, JSON.stringify(blocks));
    } catch {
      // localStorage 不可用（隐私模式等）时退化为会话内记忆
    }
  }, [blocks]);

  const [versionOpen, setVersionOpen] = useState(false);
  const [versionName, setVersionName] = useState('');

  // 归一化：Beszel 系统名大写、台账 id 小写，统一小写比（总控台 server 卡可能带大写名跳过来）
  const machineId = id ? machineIdOf(id) : null;

  useEffect(() => {
    setStatFilter(parseFilter(searchParams.get('filter')));
  }, [searchParams]);

  const applyFilter = (filter: StatFilter) => {
    setStatFilter(filter);
    const next = new URLSearchParams(searchParams);
    if (filter) next.set('filter', filter);
    else next.delete('filter');
    setSearchParams(next, { replace: true });
  };

  const loadServices = useEventCallback(async (force: boolean = false) => {
    const request = ++servicesRequest.current;
    clearTimeout(servicesPoll.current);
    try {
      const { services: data, snapshot } = await getServicesSnapshot(force);
      if (request !== servicesRequest.current) return;
      setServices(data);
      setServicesSnapshot(snapshot);
      if (snapshot.refreshing) {
        servicesPoll.current = setTimeout(() => { void loadServices(); }, 2500);
      }
    } catch (err) {
      if (request !== servicesRequest.current) return;
      console.error(err);
      toast.error('获取服务列表失败');
    } finally {
      if (request === servicesRequest.current) {
        setLoading(false);
        onLoaded?.();
      }
    }
  });

  const loadMonitor = useEventCallback(async (force: boolean = false, silent: boolean = false) => {
    const request = ++monitorRequest.current;
    clearTimeout(monitorPoll.current);
    try {
      const data = await getMonitor(force);
      if (request !== monitorRequest.current) return;
      setMonitor(data);
      if (data.snapshot?.refreshing) {
        monitorPoll.current = setTimeout(() => { void loadMonitor(false, true); }, 2500);
      }
    } catch {
      if (request !== monitorRequest.current) return;
      if (!silent) toast.error('获取监控数据失败');
    }
  });

  useEffect(() => () => {
    ++servicesRequest.current;
    ++monitorRequest.current;
    clearTimeout(servicesPoll.current);
    clearTimeout(monitorPoll.current);
  }, []);

  const loadMachines = useEventCallback(async (force: boolean = false) => {
    try {
      const data = await listMachines(force);
      setMachines(data);
    } catch {
      // Track B 未就绪时 /api/machines 返回 404，节点区保持空，不影响服务器区。
    } finally {
      setMachinesLoaded(true);
    }
  });

  useEffect(() => {
    getConfig()
      .then((cfg) => setIntervalSec(cfg.probe_interval || 600))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const force = refreshKey !== previousRefreshKey.current && refreshForce;
    previousRefreshKey.current = refreshKey;
    loadServices(force);
    loadMonitor(force);
    loadMachines();
  }, [refreshKey, refreshForce, loadServices, loadMonitor, loadMachines]);

  // J（§4.2）：单机 Beszel 事件/systemd/SMART。失败只把该区置空，**不清空服务表与机器卡**。
  useEffect(() => {
    let alive = true;
    const force = refreshKey !== previousObservabilityKey.current && refreshForce;
    previousObservabilityKey.current = refreshKey;
    getObservability(force)
      .then((data) => {
        if (alive) setObservability(data);
      })
      .catch(() => {
        if (alive) setObservability(null);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey, refreshForce]);

  // 按探测间隔自动静默刷新 Beszel（默认 10 分钟，来自 /api/config）
  useEffect(() => {
    if (!interval) return;
    let alive = true;
    const timer = window.setInterval(() => {
      loadMonitor(true, true);
      // 单机事件/systemd/SMART 与 Beszel 同节奏静默刷新（§4.2），失败只置空该区
      getObservability(true)
        .then((data) => {
          if (alive) setObservability(data);
        })
        .catch(() => {
          if (alive) setObservability(null);
        });
    }, interval * 1000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [interval, loadMonitor]);

  const stats = useMemo(() => {
    const registered = mergeRows(services, monitor.containers || []).filter((r) => r.service);
    return {
      total: services.length,
      running: registered.filter((r) => isStatusUp(r.runStatus)).length,
      healthy: services.filter((s) => s.health?.status === 'Healthy').length,
      outdated: services.filter((s) => isOutdated(s)).length,
    };
  }, [services, monitor.containers]);

  const roleMap = useMemo(() => buildRoleMap(machines), [machines]);
  // §4.4：本页「全部」视图就是服务器区，chip 只做提示不再跳转（不传 onOfflineClick → 渲染成非交互元素）
  const offlineServers = useMemo(
    () => countOfflineServers(monitor.systems || [], roleMap),
    [monitor.systems, roleMap]
  );
  // E2：节点卡的指标来自 Beszel（kr/sg 也装了 agent），按 machine id 对上
  const systemByMachine = useMemo(() => {
    const map: Record<string, MonitorSystem> = {};
    for (const sys of monitor.systems || []) {
      if (sys.machine) map[sys.machine] = sys;
    }
    return map;
  }, [monitor.systems]);

  const activeMachine = useMemo(
    () => machines.find((m) => m.id === machineId) ?? null,
    [machines, machineId]
  );
  // M2/M3：磁盘趋势**按需拉**——只有真要看它的两处才请求（server 单机视图、对比框）。
  // 后端缓存 1 小时，所以这里不跟 Beszel 同节奏轮询：多拉也不会有新数据点。
  const needCapacity = compareOpen || activeMachine?.role === 'server';
  useEffect(() => {
    if (!needCapacity) return;
    let alive = true;
    getCapacity()
      .then((data) => {
        if (alive) setCapacity(data);
      })
      .catch(() => {
        // 取不到就让磁盘趋势卡整块不渲染，不弹 toast：这不是用户发起的动作
        if (alive) setCapacity(null);
      });
    return () => {
      alive = false;
    };
  }, [needCapacity, refreshKey]);

  const serviceCounts = useMemo(() => countServicesByMachine(services), [services]);
  const machineServices = useMemo(
    () => (machineId ? filterServicesByMachine(services, machineId) : services),
    [services, machineId]
  );
  const machineContainers = useMemo(
    () =>
      machineId
        ? filterContainersByMachine(monitor.containers || [], machineId)
        : monitor.containers || [],
    [monitor.containers, machineId]
  );

  const handleAction = async (svc: Service, action: ServiceAction) => {
    const actionLabels: Record<ServiceAction, string> = {
      start: '启动', stop: '停止', restart: '重启', logs: '日志', upgrade: '升级',
    };
    const actionLabel = actionLabels[action];
    setActionLoading(true);
    try {
      const res = await runServiceAction(svc.name, action);
      if (action === 'logs') {
        setLogs(res.output || '无日志');
        setLogsOpen(true);
      } else if (res.ok) {
        if (res.warning) toast.warning(res.warning);
        else toast.success(`${actionLabel}成功`);
        await loadServices(true);
      } else {
        toast.error(`${actionLabel}失败: ${res.output || res.error || ''}`);
      }
    } catch (err: unknown) {
      toast.error(`操作失败: ${apiErrorMessage(err)}`);
    } finally {
      setActionLoading(false);
    }
  };

  const openEdit = (svc: Service) => {
    setSheetMode('edit');
    setSheetSvc(svc);
    setSheetOpen(true);
  };
  const openAdd = () => {
    setSheetMode('add');
    setSheetSvc(null);
    setSheetOpen(true);
  };
  const openVersion = (svc: Service) => {
    setVersionName(svc.name);
    setVersionOpen(true);
  };

  const openAddMachine = useEventCallback(() => {
    setMachineSheetMode('add');
    setMachineSheetId(null);
    setMachineSheetOpen(true);
  });
  const openEditMachine = useEventCallback((id: string) => {
    setMachineSheetMode('edit');
    setMachineSheetId(id);
    setMachineSheetOpen(true);
  });
  const canCompare = machines.length >= 2;
  const editableId = activeMachine?.id;
  useEffect(() => {
    const controller = {
      onAdd: openAddMachine,
      onCompare: canCompare ? () => setCompareOpen(true) : undefined,
      onEdit: editableId ? () => openEditMachine(editableId) : undefined,
    };
    setDeviceActionsController(controller);
    return () => clearDeviceActionsController(controller);
  }, [canCompare, editableId, openAddMachine, openEditMachine]);

  const handleBatchPriority = async (names: string[], priority: string) => {
    try {
      await setServicesPriority(names, priority);
      toast.success(`已更新 ${names.length} 个服务的优先级`);
      loadServices(true);
    } catch (err: unknown) {
      toast.error(`批量设置失败: ${apiErrorMessage(err)}`);
      throw err;
    }
  };

  // 契约 §7.2.5：未知 id 回落到全部并提示，不白屏。
  // machines 是异步加载的，加载完成前不判定「未知」，避免初始空数组误触发回落。
  const unknownId = machineId != null && machinesLoaded && activeMachine == null;

  const allView = (
    <>
      <StatCards
        {...stats}
        loading={loading}
        active={statFilter}
        onSelect={applyFilter}
        offlineServers={offlineServers}
      />
      <UnifiedTable
        services={services}
        containers={monitor.containers || []}
        loading={loading}
        subtitle={<>数据来源 {monitor.source === 'console_pg' ? 'Console PG' : 'Beszel'} · 自动刷新 {Math.round(interval / 60)} 分钟
          {/* 监控快照 updated_at 由后端按 UTC 输出且不带时区后缀；与服务采样同一行，统一换成本地时间。 */}
          {monitor.updated_at && <> · 上次更新 <DisplayTime value={monitor.updated_at} assumeUtc /></>}
          {monitor.snapshot?.refreshing && <> · 监控更新中</>}
          {monitor.snapshot?.error && <> · 监控更新失败，显示上次结果</>}
          {servicesSnapshot?.sampledAt && <> · 服务采样 <DisplayTime value={servicesSnapshot.sampledAt} /></>}
          {servicesSnapshot?.refreshing && <> · 服务更新中</>}
          {servicesSnapshot?.error && <> · 服务更新失败，显示上次结果</>}
        </>}
        actionDisabled={actionLoading}
        statFilter={statFilter}
        onAdd={openAdd}
        onSettings={openEdit}
        onAction={handleAction}
        onVersion={openVersion}
        onContext={setContextService}
        onBatchPriority={handleBatchPriority}
      />
    </>
  );

  return (
    <div className="space-y-3 sm:space-y-6">
      <MachineTabs
        machines={machines}
        counts={serviceCounts}
        activeId={machineId}
      />

      {monitor.error && (
        <Card>
          <CardContent className="pt-6 text-sm text-destructive">
            拉取{monitor.source === 'console_pg' ? ' Console PG ' : '监控数据'}失败: {monitor.error}
          </CardContent>
        </Card>
      )}

      {machineId == null ? (
        allView
      ) : unknownId ? (
        <>
          <Card className="border-amber-500/40 bg-amber-500/5">
            <CardContent className="pt-6 text-sm text-amber-700">
              设备「{id}」不在台账中，已回落到全部设备。
            </CardContent>
          </Card>
          {allView}
        </>
      ) : activeMachine ? (
        <MachineDetail
          machine={activeMachine}
          system={systemByMachine[machineId]}
          services={machineServices}
          containers={machineContainers}
          navigation={navigation}
          loading={loading}
          subtitle={<>{activeMachine.label} · 数据来源 {monitor.source === 'console_pg' ? 'Console PG' : 'Beszel'}
            {monitor.updated_at && <> · 监控采样 <DisplayTime value={monitor.updated_at} assumeUtc /></>}
            {monitor.snapshot?.refreshing && <> · 监控更新中</>}
            {monitor.snapshot?.error && <> · 监控更新失败，显示上次结果</>}
            {servicesSnapshot?.sampledAt && <> · 服务采样 <DisplayTime value={servicesSnapshot.sampledAt} /></>}
            {servicesSnapshot?.refreshing && <> · 服务更新中</>}
            {servicesSnapshot?.error && <> · 服务更新失败，显示上次结果</>}
          </>}
          actionDisabled={actionLoading}
          observability={observability}
          capacity={capacity}
          monitorSourceFailed={Boolean(monitor.error) || Boolean(monitor.snapshot?.error)}
          monitorSource={monitor.source}
          refreshKey={refreshKey}
          onAdd={openAdd}
          onSettings={openEdit}
          onAction={handleAction}
          onVersion={openVersion}
          onContext={setContextService}
          onBatchPriority={handleBatchPriority}
          onActionDone={() => {
            loadMonitor(true, true);
            loadMachines(true);
          }}
          onScan={activeMachine.role === 'node' ? undefined : () => setDiscoverOpen(true)}
          blocks={blocks}
          onToggleBlock={(key, value) => setBlocks((prev) => ({ ...prev, [key]: value }))}
        />
      ) : (
        // machines 还在加载：先给骨架，别把单机视图闪成「未知」
        <div className="space-y-6">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-40 w-full" />
          ))}
        </div>
      )}

      <DeviceServiceContext service={contextService ? services.find((svc) => svc.name === contextService.name && svc.machine === contextService.machine) ?? null : null}
        navigation={navigation} containers={monitor.containers}
        machine={machines.find((machine) => machine.id === contextService?.machine)}
        disabled={actionLoading} onClose={() => setContextService(null)}
        onAction={handleAction} onSettings={openEdit}
        onVersion={(svc) => { setContextService(null); openVersion(svc); }} />
      <ServiceSheet
        open={sheetOpen}
        mode={sheetMode}
        service={sheetSvc}
        machines={machines}
        defaultMachineId={machineId ?? undefined}
        onOpenChange={setSheetOpen}
        onSaved={() => loadServices(true)}
      />
      <MachineSheet
        open={machineSheetOpen}
        mode={machineSheetMode}
        machineId={machineSheetId}
        onOpenChange={setMachineSheetOpen}
        onSaved={() => loadMachines(true)}
      />
      <MachineCompare
        open={compareOpen}
        machines={machines}
        systemByMachine={systemByMachine}
        capacity={capacity}
        onOpenChange={setCompareOpen}
      />
      <LogsDialog open={logsOpen} logs={logs} onOpenChange={setLogsOpen} />
      <VersionDialog open={versionOpen} name={versionName} onOpenChange={setVersionOpen} />
      {machineId != null && activeMachine != null && (
        <DiscoverDialog
          open={discoverOpen}
          machineId={machineId}
          machineLabel={activeMachine.label}
          onOpenChange={setDiscoverOpen}
          onAdded={() => loadServices(true)}
        />
      )}
    </div>
  );
}
