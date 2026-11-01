import { afterAll, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { toast } from 'sonner';
import { DashboardRoute } from './dashboard';
import { Topbar } from '@/components/layout/topbar';
import { setDashboardEditController } from '@/components/dashboard/edit-mode';
import * as api from '@/lib/api';

/**
 * C8 / D3 编辑态回路（D1' 会话制）。
 *
 * 单独一个文件而不是并进 `dashboard.test.tsx`：D2 那份守的是「两条渲染路径怎么分流」，
 * 本文件守的是编辑会话的暂存 / 保存 / 放弃与拦截，两组用例的 mock 与视口前置条件都不一样。
 *
 * D1' 的语义变化：拖拽 / 显隐 / 重置都**暂存内存**，「保存布局」= PUT 整体替换到 DB，
 * 「放弃布局修改」= 丢弃回读基线；localStorage 不再是布局的写入目标
 * （只剩 legacy 一次性迁移，那部分在 dashboard.test.tsx / dashboard-layout-store.test.ts）。
 */

vi.mock('@/lib/api', () => ({
  getMonitor: vi.fn(),
  getObservability: vi.fn(),
  listMachines: vi.fn(),
  getAttention: vi.fn(),
  getOpsStatus: vi.fn(),
  getInspectionReportMarkdown: vi.fn(),
  apiErrorMessage: vi.fn(() => ''),
  getTailnet: vi.fn(),
  getEntryTraffic: vi.fn(),
  runMachineAction: vi.fn(),
  getDashboardLayout: vi.fn(),
  saveDashboardLayout: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const mockedMonitor = vi.mocked(api.getMonitor);
const mockedObservability = vi.mocked(api.getObservability);
const mockedMachines = vi.mocked(api.listMachines);
const mockedAttention = vi.mocked(api.getAttention);
const mockedOps = vi.mocked(api.getOpsStatus);
const mockedTailnet = vi.mocked(api.getTailnet);
const mockedEntry = vi.mocked(api.getEntryTraffic);
mockedEntry.mockResolvedValue({
  available: false, sampled_at: null, error: null, upstreams: [],
  metrics_available: false, metrics_error: null, counting_since: null, hosts: [],
});
const mockedLayout = vi.mocked(api.getDashboardLayout);
const mockedSaveLayout = vi.mocked(api.saveDashboardLayout);

/** 引擎节点够用的最小面，避免为一个测试引进整套 gridstack 类型。 */
interface TestGrid {
  opts: { staticGrid?: boolean };
  engine: { nodes: { id?: string | number; x?: number; el?: HTMLElement }[] };
  update: (el: HTMLElement, opt: { x?: number; y?: number; w?: number }) => unknown;
  updateOptions: (o: unknown) => unknown;
  _gsEventHandler: Record<string, ((e: Event, el?: HTMLElement) => void) | undefined>;
}

function gridOf(container: HTMLElement): TestGrid {
  const el = container.querySelector('.grid-stack') as (HTMLElement & { gridstack?: TestGrid }) | null;
  if (!el?.gridstack) throw new Error('网格未初始化');
  return el.gridstack;
}

/**
 * 走一次完整拖拽手势。jsdom 里合成不出真实指针拖拽，直接喊引擎登记过的
 * `dragstart` / `dragstop` 回调，中间用引擎自己的 `update` 挪节点——
 * 产品代码那条 `onDragStart → 快照`、`onDragStop → layoutFromNodes → 暂存内存`
 * 一步不少。真实指针拖拽由 Playwright 冒烟覆盖。
 */
function dragGesture(grid: TestGrid, widgetKey: string, move?: { x?: number; y?: number; w?: number }) {
  const node = grid.engine.nodes.find((n) => n.id === widgetKey);
  if (!node?.el) throw new Error(`找不到 ${widgetKey}`);
  act(() => {
    grid._gsEventHandler.dragstart?.(new Event('dragstart'), node.el);
    if (move) grid.update(node.el!, move);
    grid._gsEventHandler.dragstop?.(new Event('dragstop'), node.el);
  });
}

function setViewport(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
}

function Harness({ onRefresh }: { onRefresh?: () => void }) {
  return (
    <MemoryRouter initialEntries={['/']}>
      <Topbar onRefresh={onRefresh} />
      <Routes>
        <Route path="/" element={<DashboardRoute refreshKey={0} />} />
        <Route path="/devices/:id" element={<div>单机详情页</div>} />
      </Routes>
    </MemoryRouter>
  );
}

function pencil() {
  return screen.getByRole('button', { name: '编辑布局' });
}

/** C8-1：静态渲染树删除后总控台只剩网格一条路径，等它出现 = 等首屏骨架让位。 */
async function waitForGrid() {
  await waitFor(() => expect(document.querySelector('.grid-stack')).toBeTruthy());
}

/** 退出编辑态的可观测信号：屏蔽层全部撤走（渲染路径不再随编辑态切换）。 */
async function waitForViewMode(container: HTMLElement) {
  await waitFor(() => expect(container.querySelectorAll('[data-widget-shield]').length).toBe(0));
}

async function enterEditMode() {
  fireEvent.click(pencil());
  await screen.findByRole('button', { name: '保存布局' });
  await waitFor(() => expect(document.querySelector('.grid-stack')).toBeTruthy());
}

async function saveLayout() {
  fireEvent.click(screen.getByRole('button', { name: '保存布局' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '保存布局' })).toBeNull());
}

beforeAll(async () => {
  // DashboardGrid 走 React.lazy：先把模块拉进缓存，免得动态 import 在全量并行下
  // 跟别的 worker 抢资源超过 waitFor 默认的 1s（D2 踩过这条）。
  await import('@/components/dashboard/dashboard-grid');
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("console.dashboard.view", "full");
  // 编辑态只在 12 列档开放（见 edit-mode.ts）；jsdom 默认 1024 属 6 列档
  setViewport(1440);
  // jsdom 缺的 ResizeObserver 由 setupTests.ts 兜底（C8-1 起每个总控台用例都要用）

  mockedMonitor.mockResolvedValue({
    systems: [{ id: '1', name: 'NAS', machine: 'nas', status: 'up' }],
    containers: [],
  });
  mockedMachines.mockResolvedValue([
    { id: 'nas', label: 'NAS', role: 'server', actions: [] },
    { id: 'kr', label: '韩国节点', role: 'node', actions: [{ key: 'restart', label: '重启 sing-box' }] },
  ]);
  mockedAttention.mockResolvedValue([]);
  mockedOps.mockResolvedValue({
    running_commit: null,
    versions_last_checked: null,
    feed_last_fetched: null,
    last_backup_tag: null,
    // A7/F17：OpsStatus 扩为七键，新增键与既有四键同口径给 null 默认值
    last_deploy: null,
    last_probe_run: null,
    timed_backup: null,
  });
  mockedObservability.mockResolvedValue({
    ok: true,
    updated_at: '2026-09-06T12:00:00Z',
    probe_interval: 600,
    source_errors: [],
    machine_links: {},
    active_alerts: [],
    recent_events: [],
    systemd_services: [],
    smart_devices: [],
  });
  mockedTailnet.mockResolvedValue({
    available: false,
    sampled_at: null,
    error: null,
    machines: [],
  });
  // D1'：布局默认空表（→ 默认布局现算）；要测 override 的用例单独改写
  mockedLayout.mockResolvedValue({ items: [], updated_at: null });
  mockedSaveLayout.mockResolvedValue({ items: [], updated_at: '2026-09-06T00:00:00Z' });
});

afterAll(() => {
  setViewport(1024);
});

afterEach(() => {
  setDashboardEditController(null);
});

describe('编辑模式开关（C8/D3 + D1\' 会话制）', () => {
  test('12 列档顶栏有铅笔；点它进编辑态，什么都没改保存退出不 PUT', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();

    await enterEditMode();
    expect(container.querySelectorAll('[data-widget-shield]').length).toBeGreaterThan(0);

    await saveLayout();
    // 什么都没改 → 不落库（「进过一次编辑态」不该凭空生成一份 override）
    await waitForViewMode(container);
    expect(container.querySelector('.grid-stack')).toBeTruthy();
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });

  test('放弃布局修改：不 PUT，退出编辑态', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    fireEvent.click(screen.getByRole('button', { name: '放弃布局修改' }));

    await waitForViewMode(container);
    expect(screen.queryByRole('button', { name: '保存布局' })).toBeNull();
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });

  test('窄于 1280（6 列 / 1 列档）不给编辑入口', async () => {
    setViewport(1279);
    render(<Harness />);
    await waitForGrid();
    expect(screen.queryByRole('button', { name: '编辑布局' })).toBeNull();
  });

  test('编辑中缩窗到 12 列档以下按「放弃」处理：退出编辑态、不 PUT', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    act(() => {
      setViewport(1000);
      window.dispatchEvent(new Event('resize'));
    });

    await waitForViewMode(container);
    expect(screen.queryByRole('button', { name: '保存布局' })).toBeNull();
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });
});

describe('编辑态禁刷新（C8/D3）', () => {
  test('进编辑态顶栏刷新禁用，保存退出后恢复', async () => {
    const onRefresh = vi.fn();
    render(<Harness onRefresh={onRefresh} />);
    await waitForGrid();

    const refresh = () => screen.getByRole('button', { name: /刷新/ }) as HTMLButtonElement;
    expect(refresh().disabled).toBe(false);

    await enterEditMode();
    expect(refresh().disabled).toBe(true);
    fireEvent.click(refresh());
    expect(onRefresh).not.toHaveBeenCalled();

    await saveLayout();
    await waitFor(() => expect(refresh().disabled).toBe(false));
  });
});

describe('编辑态交互屏蔽（C8/D3）', () => {
  test('查看态点机器卡会进单机页——这是编辑态必须挡掉的那条路径', async () => {
    render(<Harness />);
    const link = await screen.findByRole('link', { name: /NAS/ });

    fireEvent.click(link);
    expect(await screen.findByText('单机详情页')).toBeTruthy();
  });

  test('编辑态每张卡都盖满屏蔽层，且屏蔽层不在卡片的 <a> 里面', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    const shields = Array.from(container.querySelectorAll('[data-widget-shield]'));
    // 默认 7 件（D4-2.3 起巡检工具条收进顶栏；C11 加入口流量）：今日关注 / 监控事件 / NAS / 韩国节点 / 运维状态 / tailnet / 入口流量
    expect(shields.length).toBe(7);
    for (const shield of shields) {
      // 盖满整个卡片宿主
      expect(shield.className).toContain('absolute');
      expect(shield.className).toContain('inset-0');
    }

    const shield = container.querySelector('[data-widget-shield="machine-card:nas"]')!;
    const link = container.querySelector('a[href="/devices/nas"]')!;
    expect(link).toBeTruthy();
    // 兄弟关系：点击落在屏蔽层上，既不冒泡进 <a>、也不落到卡内按钮
    expect(link.contains(shield)).toBe(false);
    expect(shield.contains(link)).toBe(false);
  });

  test('点屏蔽层不导航、不弹节点卡确认框，且 click 被就地按死', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    const serverShield = container.querySelector('[data-widget-shield="machine-card:nas"]')!;
    // fireEvent.click 返回「事件没被 preventDefault」——false 即屏蔽层确实按死了它
    expect(fireEvent.click(serverShield)).toBe(false);

    const nodeShield = container.querySelector('[data-widget-shield="machine-card:kr"]')!;
    expect(fireEvent.click(nodeShield)).toBe(false);

    expect(screen.queryByText('单机详情页')).toBeNull();
    expect(screen.queryByText(/确认在 .* 上执行/)).toBeNull();
    expect(container.querySelector('.grid-stack')).toBeTruthy();
  });

  test('退出编辑态后屏蔽层全部撤走', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();
    expect(container.querySelectorAll('[data-widget-shield]').length).toBe(7);

    await saveLayout();
    await waitFor(() => expect(container.querySelectorAll('[data-widget-shield]').length).toBe(0));
  });
});

describe('编辑会话：暂存 → 保存 = PUT（D1\'）', () => {
  test('拖拽只暂存内存；保存后 PUT 整体替换，重新挂载后位置从 DB 回来', async () => {
    const first = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    const grid = gridOf(first.container);
    expect(grid.engine.nodes.find((n) => n.id === 'machine-card:nas')!.x).toBe(0);

    dragGesture(grid, 'machine-card:nas', { x: 6 });

    // 会话制：手势结束不写 localStorage（写入目标只剩 DB）
    expect(localStorage.getItem('console.dashboard.layout')).toBeNull();
    expect(mockedSaveLayout).not.toHaveBeenCalled();

    await saveLayout();

    expect(mockedSaveLayout).toHaveBeenCalledTimes(1);
    const payload = mockedSaveLayout.mock.calls[0][0];
    const nas = payload.find((it) => it.widget_key === 'machine-card:nas');
    // y 是引擎 float:false 压实后的真实行号（关注/事件之后的第 3 行 = 2），不是名义 ROW_STRIDE 值
    expect(nas).toEqual({ widget_key: 'machine-card:nas', x: 6, y: 2, w: 3, hidden: false });

    first.unmount();

    // 重新挂载 = 刷新页面：GET 返回刚保存的行 → 网格路径，位置从 DB 里来
    mockedLayout.mockResolvedValue({
      items: [{ widget_key: 'machine-card:nas', x: 6, y: 0, w: 3, hidden: false }],
      updated_at: '2026-09-06T00:00:00Z',
    });
    const second = render(<Harness />);
    await waitFor(() => expect(second.container.querySelector('.grid-stack')).toBeTruthy());
    await waitFor(() =>
      expect(
        gridOf(second.container).engine.nodes.find((n) => n.id === 'machine-card:nas')?.x
      ).toBe(6)
    );
    // 网格路径不再有分区标题（D2 已裁决接受）
    expect(screen.queryByText('设备与服务')).toBeNull();
  });

  test('没挪动的手势不标脏——保存不 PUT，3px 阈值内的误触不该凭空生成 override', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    dragGesture(gridOf(container), 'machine-card:nas'); // dragstart → dragstop，中间什么都没动

    await saveLayout();
    await waitForViewMode(container);
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });

  test('放弃丢弃本轮拖拽：不 PUT，坐标回滚到基线', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    dragGesture(gridOf(container), 'machine-card:nas', { x: 6 });
    expect(gridOf(container).engine.nodes.find((n) => n.id === 'machine-card:nas')!.x).toBe(6);

    fireEvent.click(screen.getByRole('button', { name: '放弃布局修改' }));

    await waitForViewMode(container);
    // 基线是「没有 override」→ 回默认布局现算的位置
    await waitFor(() =>
      expect(gridOf(container).engine.nodes.find((n) => n.id === 'machine-card:nas')?.x).toBe(0)
    );
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });

  test('保存失败 → 留在编辑态报错，重试成功后退出', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    dragGesture(gridOf(container), 'machine-card:nas', { x: 6 });

    mockedSaveLayout.mockRejectedValueOnce(new Error('boom'));
    fireEvent.click(screen.getByRole('button', { name: '保存布局' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    // 还在编辑态：保存 / 放弃按钮都在，改动可重试
    expect(screen.getByRole('button', { name: '保存布局' })).toBeTruthy();
    expect(container.querySelector('.grid-stack')).toBeTruthy();

    await saveLayout();
    expect(mockedSaveLayout).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(container.querySelector('.grid-stack')).toBeTruthy());
  });

  /**
   * 浏览器实测踩过：`editing` 一旦进 grid options，wrapper 就会调 `updateOptions()`，
   * 而它最后一句是 `load(children)`——children 按 C8 契约不带 `h`，一 load 全部卡片
   * 高度打回 1 格塌成 10px 条，`resizeToContent` 又不会自己回来。
   * 所以这条断言的是「切编辑态期间 `updateOptions` 一次都不许被调用」。
   */
  test('切编辑态只 setStatic，不许触发 updateOptions（它会 load(children) 打掉卡片高度）', async () => {
    mockedLayout.mockResolvedValue({
      items: [{ widget_key: 'machine-card:nas', x: 6, y: 0, w: 3, hidden: false }],
      updated_at: '2026-09-06T00:00:00Z',
    });
    const { container } = render(<Harness />);
    await waitFor(() => expect(container.querySelector('.grid-stack')).toBeTruthy());

    const before = gridOf(container);
    expect(before.opts.staticGrid).toBe(true);
    const updateOptions = vi.spyOn(before, 'updateOptions');

    await enterEditMode();
    expect(gridOf(container)).toBe(before); // 同一个引擎实例 = 没重建
    expect(gridOf(container).opts.staticGrid).toBeFalsy();
    expect(updateOptions).not.toHaveBeenCalled();

    await saveLayout();
    await waitFor(() => expect(gridOf(container).opts.staticGrid).toBe(true));
    expect(gridOf(container)).toBe(before);
    expect(updateOptions).not.toHaveBeenCalled();
    updateOptions.mockRestore();
  });

  test('重置默认布局：暂存成空，保存时 PUT items=[]，退出后回默认布局', async () => {
    mockedLayout.mockResolvedValue({
      items: [
        { widget_key: 'machine-card:nas', x: 6, y: 0, w: 3, hidden: false },
        { widget_key: 'tailnet', x: 0, y: 60, w: 12, hidden: true },
      ],
      updated_at: '2026-09-06T00:00:00Z',
    });

    const { container } = render(<Harness />);
    // 有 override → 一进来就是网格路径
    await waitFor(() => expect(container.querySelector('.grid-stack')).toBeTruthy());
    await enterEditMode();
    expect(container.querySelectorAll('[data-widget-shield]').length).toBe(6); // tailnet 被隐藏（默认 7 件 - 1）

    fireEvent.click(screen.getByRole('button', { name: '重置默认布局' }));

    await waitFor(() => expect(container.querySelectorAll('[data-widget-shield]').length).toBe(7));
    expect(mockedSaveLayout).not.toHaveBeenCalled(); // 重置只是暂存，保存才落库

    await saveLayout();

    expect(mockedSaveLayout).toHaveBeenCalledWith([]);
    await waitForViewMode(container);
    // 回默认布局：被隐藏的 tailnet 找回，7 件全在
    await waitFor(() => expect(gridOf(container).engine.nodes.length).toBe(7));
  });
});

describe('widget 显隐（C8/D3，D1\' 保存落库）', () => {
  test('⋮ →「隐藏」暂存内存，保存后 PUT 带 hidden 行；顶栏「添加 widget」找回归位', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    fireEvent.click(screen.getByRole('button', { name: 'NAS 的布局菜单' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '隐藏' }));

    await waitFor(() =>
      expect(container.querySelector('[data-widget-shield="machine-card:nas"]')).toBeNull()
    );
    expect(container.querySelectorAll('[data-widget-shield]').length).toBe(6); // 默认 7 件 - 隐藏 NAS
    expect(mockedSaveLayout).not.toHaveBeenCalled(); // 会话制：还没落库

    await saveLayout();

    const payload = mockedSaveLayout.mock.calls[0][0];
    // nas 从未挪过 → hidden 行坐标取默认布局现算值（D4-2.3 起默认布局无巡检，y=24）
    expect(payload).toContainEqual({ widget_key: 'machine-card:nas', x: 0, y: 24, w: 3, hidden: true });

    // 找回：重新进入编辑态，添加 widget 列出被隐藏的卡
    await enterEditMode();
    fireEvent.click(screen.getByRole('button', { name: '添加 widget' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'NAS' }));
    await waitFor(() =>
      expect(container.querySelector('[data-widget-shield="machine-card:nas"]')).toBeTruthy()
    );

    await saveLayout();
    // 找回后 NAS 回到「没有任何自定义」的干净形态：渲染走 default⊕∅，DB 里不需要它的行
    // （与 D3「空集合=删键、没设过只有一种落盘形态」同语义，只是落点是 DB）
    const payload2 = mockedSaveLayout.mock.calls[1][0];
    expect(payload2.find((it) => it.widget_key === 'machine-card:nas')).toBeUndefined();
  });

  test('只隐藏、没挪过位置时也走网格路径；重挂载后仍不显示那张卡', async () => {
    const first = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    fireEvent.click(screen.getByRole('button', { name: 'Tailscale 网络 的布局菜单' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '隐藏' }));
    await waitFor(() =>
      expect(first.container.querySelector('[data-widget-shield="tailnet"]')).toBeNull()
    );

    await saveLayout();
    first.unmount();

    // 重挂载：DB 里只有一条 hidden 行 → 网格路径、tailnet 不在节点里
    mockedLayout.mockResolvedValue({
      items: [{ widget_key: 'tailnet', x: 0, y: 60, w: 12, hidden: true }],
      updated_at: '2026-09-06T00:00:00Z',
    });
    const second = render(<Harness />);
    await waitFor(() => expect(second.container.querySelector('.grid-stack')).toBeTruthy());
    expect(localStorage.getItem('console.dashboard.layout')).toBeNull();
    await waitFor(() =>
      expect(gridOf(second.container).engine.nodes.map((n) => n.id)).not.toContain('tailnet')
    );
  });

  test('拖过再隐藏：这一轮挪出来的位置不会被隐藏引发的重算冲掉，保存后一起落库', async () => {
    const { container } = render(<Harness />);
    await waitForGrid();
    await enterEditMode();

    dragGesture(gridOf(container), 'machine-card:nas', { x: 6 });

    fireEvent.click(screen.getByRole('button', { name: 'Tailscale 网络 的布局菜单' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '隐藏' }));

    await waitFor(() => expect(container.querySelector('[data-widget-shield="tailnet"]')).toBeNull());
    await waitFor(() =>
      expect(gridOf(container).engine.nodes.find((n) => n.id === 'machine-card:nas')?.x).toBe(6)
    );

    await saveLayout();
    const payload = mockedSaveLayout.mock.calls[0][0];
    expect(payload).toContainEqual({ widget_key: 'machine-card:nas', x: 6, y: 2, w: 3, hidden: false });
    expect(payload).toContainEqual(
      expect.objectContaining({ widget_key: 'tailnet', hidden: true })
    );
  });
});
