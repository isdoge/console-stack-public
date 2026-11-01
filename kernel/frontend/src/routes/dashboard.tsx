import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { DisplayTime } from '@/components/ui/display-time';
import { DashboardOverview } from '@/components/dashboard/dashboard-overview';
import { AttentionStrip } from '@/components/dashboard/attention-strip';
import {
  clearDashboardEditController,
  setDashboardEditController,
  useLayoutEditable,
} from '@/components/dashboard/edit-mode';
import type { DashboardEditController } from '@/components/dashboard/edit-mode';
import {
  clearInspectionActionsController,
  setInspectionActionsController,
} from '@/components/dashboard/inspection-actions';
import type { InspectionActionsController } from '@/components/dashboard/inspection-actions';
import { widgetLabel } from '@/components/dashboard/hidden-model';
import {
  DashboardSkeleton,
  SERVER_SKELETON_COUNT,
} from '@/components/dashboard/dashboard-skeleton';
import {
  ORPHAN_SYSTEM_PREFIX,
  buildDefaultLayout,
  machineWidgetId,
  mergeLayout,
} from '@/components/dashboard/layout-model';
import type { LayoutItem, WidgetKey } from '@/components/dashboard/layout-model';
import { OpsStatusCard } from '@/components/dashboard/ops-status-card';
import { EntryTrafficCard } from '@/components/dashboard/entry-traffic-card';
import { TailnetCard } from '@/components/dashboard/tailnet-card';
import { NodeMachineCard, ServerMachineCard } from '@/components/dashboard/widgets';
import { ObservabilitySummary } from '@/components/monitor/observability-summary';
import {
  apiErrorMessage,
  getInspectionReportMarkdown,
  getMonitor,
  getObservability,
  listMachines,
} from '@/lib/api';
import {
  clearLegacyState,
  loadDashboardLayout,
  persistDashboardLayout,
  readLegacyState,
  statesEqual,
} from '@/lib/dashboard-layout-store';
import type { DashboardLayoutState } from '@/lib/dashboard-layout-store';
import { EMPTY_LAYOUT_STATE } from '@/lib/dashboard-layout-store';
import { useEventCallback } from '@/lib/use-event-callback';
import { copyText } from '@/lib/utils';
import type { Machine, MonitorPayload, MonitorSystem, ObservabilityPayload } from '@/lib/types';

/**
 * 网格引擎（gridstack + 它的 CSS）按需加载：C8-1 起总控台只有网格一条渲染路径，
 * 但首屏要等布局/台账/监控三份数据，这段时间正好用来并行下载引擎 chunk，
 * 主 bundle 也不必把它算进首包。App.tsx 的路由也是同一套 lazy 写法。
 */
const DashboardGrid = lazy(() =>
  import('@/components/dashboard/dashboard-grid').then((m) => ({ default: m.DashboardGrid }))
);

interface Props {
  refreshKey: number;
  refreshForce?: boolean;
  onLoaded?: () => void;
}

/** 文件名用本地时间：这是给人贴回 daily 的，报告正文内部才是 UTC。 */
export function inspectionFileName(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  const stamp =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `巡检报告-${stamp}.md`;
}

/** 触发浏览器下载；用完立刻 revoke，不留 blob URL。 */
function downloadMarkdown(text: string, fileName: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function DashboardRoute({ refreshKey, refreshForce = true, onLoaded }: Props) {
  const navigate = useNavigate();
  const handleLoaded = useEventCallback(() => onLoaded?.());
  const [monitor, setMonitor] = useState<MonitorPayload>({ systems: [], containers: [] });
  const [machines, setMachines] = useState<Machine[]>([]);
  const [observability, setObservability] = useState<ObservabilityPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [monitorFailed, setMonitorFailed] = useState(false);
  const [machinesFailed, setMachinesFailed] = useState(false);
  const [overview, setOverview] = useState(() => {
    try { return localStorage.getItem('console.dashboard.view') !== 'full'; } catch { return true; }
  });
  // 同时只允许一个动作在跑：两次点击会各自向后端要一份报告，采样时刻还不一样
  const [reportBusy, setReportBusy] = useState<'copy' | 'download' | null>(null);
  // D1'：布局来自 DB（GET /api/dashboard-layout），不再是挂载时同步读 localStorage。
  // C8-1 起没有第二条渲染路径：GET 回来之前显示骨架，回来之后一律网格渲染。
  const [layoutOverride, setLayoutOverride] = useState<LayoutItem[] | null>(null);
  const [hidden, setHidden] = useState<WidgetKey[]>([]);
  // GET 失败 → 只读降级：按默认布局现算显示、编辑入口不开放（不回退 localStorage 当写入目标）
  const [layoutReadOnly, setLayoutReadOnly] = useState(false);
  // 布局请求是否还在途；成败都置 false（失败走只读降级，不把页面卡在骨架上）
  const [layoutLoading, setLayoutLoading] = useState(true);
  // 一次性迁移只在台账/Beszel 都到位后跑（hidden 行要补默认坐标，defaults 不完整时不跑）
  const [machinesLoaded, setMachinesLoaded] = useState(false);
  const previousRefreshKey = useRef(refreshKey);
  const previousObservabilityKey = useRef(refreshKey);
  // C8 / D3：布局编辑态
  const [editing, setEditing] = useState(false);
  const [savingLayout, setSavingLayout] = useState(false);
  const editable = useLayoutEditable();
  const editingActive = editing && editable;

  const runReport = useCallback(async (mode: 'copy' | 'download') => {
    setReportBusy(mode);
    try {
      const markdown = await getInspectionReportMarkdown();
      if (mode === 'copy') {
        await copyText(markdown);
        toast.success('巡检报告已复制到剪贴板');
      } else {
        downloadMarkdown(markdown, inspectionFileName());
        toast.success('巡检报告已下载');
      }
    } catch (err) {
      toast.error(apiErrorMessage(err) || '生成巡检报告失败');
    } finally {
      setReportBusy(null);
    }
  }, []);

  // D4-2.3：巡检报告动作登记给顶栏（模块级订阅源，App.tsx 零改动）
  const inspectionController = useMemo<InspectionActionsController>(
    () => ({ busy: reportBusy, onRun: (mode) => void runReport(mode) }),
    [reportBusy, runReport]
  );
  useEffect(() => {
    setInspectionActionsController(inspectionController);
    return () => clearInspectionActionsController(inspectionController);
  }, [inspectionController]);

  // node 动作成功后局部刷新：探活状态与 Beszel 指标都可能变化
  const reloadMonitor = useCallback(async (force = false) => {
    try {
      const data = await getMonitor(force);
      setMonitor(data);
    } catch (err) {
      console.error(err);
    }
  }, []);

  const loadMachines = useCallback(async (force = false) => {
    try {
      const data = await listMachines(force);
      setMachines(data);
    } catch {
      // Track B 未就绪时 /api/machines 404 → 节点区保持空，不影响服务器区。
    }
  }, []);

  // ---- C8 / D1'：布局加载 ----

  useEffect(() => {
    let alive = true;
    loadDashboardLayout().then((result) => {
      if (!alive) return;
      baselineRef.current = result.state;
      setLayoutOverride(result.state.layoutOverride);
      setHidden(result.state.hidden);
      setLayoutReadOnly(result.readOnly);
      setLayoutLoading(false);
      if (result.error) toast.error(result.error);
    });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    let poll: ReturnType<typeof setTimeout> | undefined;
    const force = refreshKey !== previousRefreshKey.current && refreshForce;
    previousRefreshKey.current = refreshKey;
    const load = (refresh: boolean) => {
      getMonitor(refresh)
        .then((mon) => {
          if (!alive) return;
          setMonitor(mon);
          setMonitorFailed(false);
          if (mon.snapshot?.refreshing) poll = setTimeout(() => load(false), 2500);
        })
        .catch((err) => {
          if (!alive) return;
          console.error(err);
          setMonitorFailed(true);
          toast.error('获取总览数据失败');
        })
        .finally(() => {
          if (!alive) return;
          setLoading(false);
          handleLoaded();
        });
    };
    load(force);
    return () => {
      alive = false;
      clearTimeout(poll);
    };
  }, [refreshKey, refreshForce, handleLoaded]);

  useEffect(() => {
    let alive = true;
    listMachines()
      .then((m) => {
        if (alive) { setMachines(m); setMachinesFailed(false); }
      })
      .catch(() => { if (alive) setMachinesFailed(true); })
      .finally(() => {
        // 无论成败都算「已就位」：defaults 不会再变多，一次性迁移可以跑了
        if (alive) setMachinesLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  // J（§4.2）：告警/事件摘要。失败只把摘要置空，**不清空机器卡与总览**（扩展接口独立性）。
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

  // 首屏骨架按台账 server 数铺格；台账未到退回兜底常量（I0-4）
  const serverSkeletonCount = machines.some((m) => m.role === 'server')
    ? machines.filter((m) => m.role === 'server').length
    : SERVER_SKELETON_COUNT;
  // E2：节点卡的指标来自 Beszel（kr/sg 也装了 agent），按 machine id 对上
  const systemByMachine = useMemo(() => {
    const map: Record<string, MonitorSystem> = {};
    for (const sys of monitor.systems || []) {
      if (sys.machine) map[sys.machine] = sys;
    }
    return map;
  }, [monitor.systems]);
  // server 卡要台账数据（OS/地区/价格/流量/色带），只有 system 会全部退化（§4.3）
  const machineById = useMemo(() => {
    const map: Record<string, Machine> = {};
    for (const m of machines) map[m.id] = m;
    return map;
  }, [machines]);

  const onNodeActionDone = useCallback(() => {
    reloadMonitor(true);
    loadMachines(true);
  }, [reloadMonitor, loadMachines]);

  // ---- C8 / D2：网格渲染路径 ----

  // 台账里没有、只在 Beszel 里出现的系统按 Beszel id 索引（widget key 里带 `sys:` 前缀）
  const systemById = useMemo(() => {
    const map: Record<string, MonitorSystem> = {};
    for (const sys of monitor.systems || []) map[sys.id] = sys;
    return map;
  }, [monitor.systems]);

  // 当前默认布局：迁移、保存（hidden 行补坐标）、渲染三处共用同一份
  const defaultItems = useMemo(
    () => buildDefaultLayout({ machines, systems: monitor.systems || [] }),
    [machines, monitor.systems]
  );

  /**
   * localStorage → DB 一次性迁移（D1'）。
   * 等台账 + Beszel 都到位再跑：legacy 里「隐藏了但没挪过」的 widget 要补默认坐标，
   * defaults 不完整时跑会把它当成孤儿 key 丢掉。PUT 成功才清旧键，失败保留待重试
   * （不清空，防丢用户布局）；编辑中不迁移，避免把进行中的会话状态冲掉。
   */
  const migrationAttemptedRef = useRef(false);
  useEffect(() => {
    if (layoutReadOnly || editingActive || migrationAttemptedRef.current) return;
    if (!machinesLoaded || loading) return;
    migrationAttemptedRef.current = true;
    const legacy = readLegacyState();
    if (!legacy) return;
    if (statesEqual(legacy, baselineRef.current)) {
      clearLegacyState(); // DB 已有一致内容（如别的设备迁过），旧键直接退役
      return;
    }
    persistDashboardLayout(legacy, defaultItems)
      .then(() => {
        clearLegacyState();
        baselineRef.current = legacy;
        if (!editingRef.current) {
          setLayoutOverride(legacy.layoutOverride);
          setHidden(legacy.hidden);
        }
      })
      .catch(() => {
        // 保留旧键下次启动重试；本次按 legacy 显示（只降级显示，不降级写入目标）
        if (!editingRef.current) {
          setLayoutOverride(legacy.layoutOverride);
          setHidden(legacy.hidden);
        }
        toast.error('布局迁移失败：本次仍按原布局显示，下次打开自动重试');
      });
  }, [layoutReadOnly, editingActive, machinesLoaded, loading, defaultItems]);

  // ---- C8 / D3 编辑态（D1' 起改会话制：改动暂存内存，保存 = PUT，放弃 = 回读 DB）----

  /**
   * 拖拽/缩放的结果只挂在 ref 上，**编辑期间不进 state**：
   * `layoutOverride` 一变 `layout` 就重算，签名一变 `<DashboardGrid>` 整块重建，
   * 正在编辑的人会看到网格闪一下。真正需要重算布局的时刻（隐藏/找回/重置/保存）
   * 再由 `commitPending` 一次性并进 state。
   */
  const pendingLayoutRef = useRef<LayoutItem[] | null>(null);
  // 本轮编辑是否改过东西：没改就不 PUT（「进过编辑态但什么都没改」不落库）
  const dirtyRef = useRef(false);
  /**
   * 强制重挂网格的「代号」。
   *
   * 「放弃 / 重置」要把引擎里本轮的临时改动扔掉，可拖拽结果按会话制只躺在
   * `pendingLayoutRef` 里、没进 state——`layout` 与它的签名都没变，
   * `<DashboardGrid>` 自然不会重建，屏幕上就还留着刚拖出来的位置。
   * D2/D3 时代这一步是「网格 ⇄ 静态渲染树」切换顺带做掉的，C8-1 删掉静态树后
   * 必须显式来一次：代号 +1 → key 变 → 网格重挂 → 按 `layout` 重新铺格。
   */
  const [gridGeneration, setGridGeneration] = useState(0);
  const remountGrid = useCallback(() => setGridGeneration((n) => n + 1), []);
  // 最近一次成功持久化（加载 / 保存 / 迁移）的状态；放弃与窄屏退出按它回滚
  const baselineRef = useRef<DashboardLayoutState>(EMPTY_LAYOUT_STATE);
  // 迁移等异步回调里判断「此刻是否在编辑」，避免冲掉进行中的会话状态
  const editingRef = useRef(false);
  useEffect(() => {
    editingRef.current = editingActive;
  }, [editingActive]);

  const commitPending = useCallback(() => {
    if (!pendingLayoutRef.current) return;
    setLayoutOverride(pendingLayoutRef.current);
    pendingLayoutRef.current = null;
  }, []);

  const handleLayoutChange = useCallback((items: LayoutItem[]) => {
    // 会话制：不再像 D3 那样手势结束即写 localStorage，只暂存内存 + 标脏
    pendingLayoutRef.current = items;
    dirtyRef.current = true;
  }, []);

  /** 进入编辑态（出口只有保存 / 放弃，铅笔不再是开关）。 */
  const enterEditing = useCallback(() => {
    dirtyRef.current = false;
    pendingLayoutRef.current = null;
    setEditing(true);
  }, []);

  const saveEditing = useCallback(() => {
    const override = pendingLayoutRef.current ?? layoutOverride;
    const state: DashboardLayoutState = { layoutOverride: override, hidden };
    if (!dirtyRef.current) {
      pendingLayoutRef.current = null;
      setEditing(false);
      return;
    }
    setSavingLayout(true);
    persistDashboardLayout(state, defaultItems)
      .then(() => {
        pendingLayoutRef.current = null;
        dirtyRef.current = false;
        baselineRef.current = state;
        setLayoutOverride(override);
        // 迁移失败残留的旧键随首次成功保存一并退役（DB 从此是唯一权威）
        clearLegacyState();
        setEditing(false);
      })
      .catch((err) => {
        // 留在编辑态：pending 不清、改动不丢，可直接重试或放弃
        toast.error(apiErrorMessage(err) || '布局保存失败');
      })
      .finally(() => setSavingLayout(false));
  }, [layoutOverride, hidden, defaultItems]);

  const cancelEditing = useCallback(() => {
    pendingLayoutRef.current = null;
    dirtyRef.current = false;
    // 丢弃本轮改动，回读最近一次成功持久化的状态（不重复打 GET）
    setLayoutOverride(baselineRef.current.layoutOverride);
    setHidden(baselineRef.current.hidden);
    setEditing(false);
    remountGrid(); // 引擎里本轮拖出来的位置一并丢掉
  }, [remountGrid]);

  /** 重置默认布局：会话制下是「暂存成空」，保存时 PUT items=[] 才真正落库。 */
  const resetLayout = useCallback(() => {
    pendingLayoutRef.current = null;
    dirtyRef.current = true;
    setLayoutOverride(null);
    setHidden([]);
    remountGrid(); // 同上：卡片当场归位，不等保存
  }, [remountGrid]);

  const hideWidget = useCallback(
    (key: WidgetKey) => {
      // 先把拖拽结果并进来：否则这一轮挪过的位置会被隐藏引发的重算冲掉
      commitPending();
      dirtyRef.current = true;
      setHidden((prev) => (prev.includes(key) ? prev : [...prev, key]));
    },
    [commitPending]
  );

  const showWidget = useCallback(
    (key: WidgetKey) => {
      commitPending();
      dirtyRef.current = true;
      setHidden((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : prev));
    },
    [commitPending]
  );

  // 视口缩到 12 列档以下（编辑态存储契约只认 12 列坐标）时自动退出编辑态；
  // 会话制下等价于「放弃」——未保存的改动丢弃回滚到基线
  useEffect(() => {
    if (editable) return;
    pendingLayoutRef.current = null;
    dirtyRef.current = false;
    setEditing(false);
    setLayoutOverride(baselineRef.current.layoutOverride);
    setHidden(baselineRef.current.hidden);
    // 只有「正编辑着被缩窄」才需要重挂：一进来就是窄屏的用户没有要丢弃的改动，
    // 白重建一次网格纯属闪屏。这里看的是 `editing` 而不是 `editingActive`
    // ——后者已经被 `editable` 拉成 false 了。
    if (editing) remountGrid();
  }, [editable, editing, remountGrid]);

  const labelOf = useCallback(
    (key: WidgetKey) => widgetLabel(key, { machines, systems: monitor.systems || [] }),
    [machines, monitor.systems]
  );

  const hiddenOptions = useMemo(
    () => hidden.map((key) => ({ key, label: labelOf(key) })),
    [hidden, labelOf]
  );

  const editController = useMemo<DashboardEditController>(
    () => ({
      editing: editingActive,
      enter: enterEditing,
      save: saveEditing,
      cancel: cancelEditing,
      saving: savingLayout,
      reset: resetLayout,
      hidden: hiddenOptions,
      show: showWidget,
    }),
    [
      editingActive,
      enterEditing,
      saveEditing,
      cancelEditing,
      savingLayout,
      resetLayout,
      hiddenOptions,
      showWidget,
    ]
  );

  // 顶栏的编辑入口由总控台登记：窄屏 / 布局只读降级（GET 失败）都不登记
  useEffect(() => {
    if (!editable || layoutReadOnly) return;
    setDashboardEditController(editController);
    return () => clearDashboardEditController(editController);
  }, [editable, layoutReadOnly, editController]);

  // 只读降级（GET 失败）时隐藏集合一并降级为「全显示」：布局服务不可用时
  // 宁可多显示一张卡，也不要让人对着一块缺角的总控台猜是不是坏了。
  const hiddenSet = useMemo(
    () => (layoutReadOnly ? new Set<WidgetKey>() : new Set(hidden)),
    [hidden, layoutReadOnly]
  );

  /**
   * C8-1：唯一渲染路径。`default ⊕ override` 现算 → 去掉隐藏项，
   * 没有 override（新用户 / 重置过 / GET 失败降级）时就是 `buildDefaultLayout` 的结果。
   */
  const layout = useMemo(
    () => mergeLayout(defaultItems, layoutOverride).filter((item) => !hiddenSet.has(item.key)),
    [layoutOverride, hiddenSet, defaultItems]
  );

  const renderWidget = useCallback(
    (key: WidgetKey): ReactNode => {
      switch (key) {
        case 'attention':
          return <AttentionStrip refreshKey={refreshKey} />;
        case 'monitor-events':
          return <ObservabilitySummary data={observability} machines={machines} />;
        case 'ops-status':
          return <OpsStatusCard refreshKey={refreshKey} />;
        case 'tailnet':
          return <TailnetCard refreshKey={refreshKey} />;
        case 'caddy-entry':
          return <EntryTrafficCard refreshKey={refreshKey} />;
        default:
          break;
      }
      const id = machineWidgetId(key);
      if (!id) return null;
      if (id.startsWith(ORPHAN_SYSTEM_PREFIX)) {
        return <ServerMachineCard system={systemById[id.slice(ORPHAN_SYSTEM_PREFIX.length)]} />;
      }
      const machine = machineById[id];
      if (!machine) return null;
      if (machine.role === 'node') {
        return (
          <NodeMachineCard
            machine={machine}
            system={systemByMachine[id]}
            onOpen={() => navigate(`/devices/${machine.id}`)}
            onActionDone={onNodeActionDone}
          />
        );
      }
      // 台账有、Beszel 没有的服务器在这里也占一格（指标整体退化为 —）：
      // 默认布局按台账铺格，Beszel 挂掉时总控台不塌。
      return <ServerMachineCard system={systemByMachine[id]} machine={machine} />;
    },
    [
      refreshKey,
      observability,
      machines,
      systemById,
      machineById,
      systemByMachine,
      navigate,
      onNodeActionDone,
    ]
  );

  /*
   * C8-1：唯一渲染路径。布局（DB）/ 台账 / 监控三份数据都到位才铺网格——
   * 没齐就渲染会先缺机器卡再补进来，布局签名一变整块网格重建（首屏闪一下）。
   * 三份都有「失败也置位」的出口（GET 失败→只读降级、monitor/machines 的 finally），
   * 所以任何一份挂掉都只是进默认布局，不会把页面卡在骨架上。
   */
  if (layoutLoading || (!overview && (loading || !machinesLoaded))) {
    return <>
      <h1 className="text-lg font-semibold">总控台</h1>
      <DashboardSkeleton serverCount={serverSkeletonCount} />
    </>;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">总控台</h1>
          <p className="text-xs text-muted-foreground">{editingActive ? '正在编辑完整布局' : overview ? '先看关注与入口，设备和运行详情按需展开' : '完整总览 · 保留自定义布局'}</p>
        </div>
        <div role="group" aria-label="首页显示模式" className="flex gap-1">
          {([true, false] as const).map((simple) => (
            <Button
              key={String(simple)}
              size="sm"
              className="min-h-11"
              disabled={editingActive}
              variant={(editingActive ? !simple : overview === simple) ? 'secondary' : 'ghost'}
              aria-pressed={editingActive ? !simple : overview === simple}
              onClick={() => {
                setOverview(simple);
                try { localStorage.setItem('console.dashboard.view', simple ? 'overview' : 'full'); } catch { /* 会话内继续 */ }
              }}
            >{simple ? '简览' : '完整总览'}</Button>
          ))}
        </div>
      </div>
      {(monitor.error || monitorFailed || machinesFailed) && (
        <Card>
          <CardContent className="pt-6 text-sm text-destructive">
            {monitor.error ? `拉取 Beszel 失败: ${monitor.error}` : machinesFailed ? '设备台账读取失败，请刷新重试' : '监控读取失败，保留上次数据，请刷新重试'}
          </CardContent>
        </Card>
      )}
      {monitor.updated_at && (
        <p className="text-xs text-muted-foreground">
          监控采样 <DisplayTime value={monitor.updated_at} />
          {monitor.snapshot?.refreshing && ' · 更新中'}
          {monitor.snapshot?.error && ' · 更新失败，显示上次结果'}
        </p>
      )}
      {overview && !editingActive ? (
        <DashboardOverview
          layout={layout}
          machines={machines}
          systems={monitor.systems || []}
          observability={observability}
          monitorUnavailable={monitorFailed || !!monitor.error}
          renderWidget={renderWidget}
          labelOf={labelOf}
        />
      ) : (
        <>
      {/* 引擎 chunk 还在下载时继续显示骨架，不要先塌成一片空白再弹出网格 */}
      <Suspense fallback={<DashboardSkeleton serverCount={serverSkeletonCount} />}>
        <DashboardGrid
          key={gridGeneration}
          layout={layout}
          renderWidget={renderWidget}
          editing={editingActive}
          onLayoutChange={handleLayoutChange}
          onHideWidget={hideWidget}
          labelOf={labelOf}
        />
      </Suspense>
        </>
      )}
    </div>
  );
}
