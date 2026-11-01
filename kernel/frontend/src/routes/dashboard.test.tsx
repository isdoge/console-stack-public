import { afterAll, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { toast } from 'sonner';
import { DashboardRoute, inspectionFileName } from './dashboard';
import { Topbar } from '@/components/layout/topbar';
import * as api from '@/lib/api';
import { copyText } from '@/lib/utils';

vi.mock('@/lib/api', () => ({
  getMonitor: vi.fn(),
  getObservability: vi.fn(),
  listMachines: vi.fn(),
  getAttention: vi.fn(),
  getOpsStatus: vi.fn(),
  // C1：巡检报告按钮用到；漏在这里会让整个 mock 模块缺导出而直接报错
  getInspectionReportMarkdown: vi.fn(),
  apiErrorMessage: vi.fn(() => ''),
  getTailnet: vi.fn(),
  getEntryTraffic: vi.fn(),
  // C8 / D1'：布局升库
  getDashboardLayout: vi.fn(),
  saveDashboardLayout: vi.fn(),
}));

// cn 仍要真实实现（子组件全在用），只把 copyText 换掉：
// jsdom 既没有 navigator.clipboard 也没有 document.execCommand，真跑会炸
vi.mock('@/lib/utils', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils')>()),
  copyText: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const mockedMonitor = vi.mocked(api.getMonitor);
const mockedObservability = vi.mocked(api.getObservability);
const mockedMachines = vi.mocked(api.listMachines);
const mockedAttention = vi.mocked(api.getAttention);
const mockedOps = vi.mocked(api.getOpsStatus);
const mockedReport = vi.mocked(api.getInspectionReportMarkdown);
const mockedErrorMessage = vi.mocked(api.apiErrorMessage);
const mockedCopy = vi.mocked(copyText);
const mockedTailnet = vi.mocked(api.getTailnet);
const mockedEntry = vi.mocked(api.getEntryTraffic);
mockedEntry.mockResolvedValue({
  available: false, sampled_at: null, error: null, upstreams: [],
  metrics_available: false, metrics_error: null, counting_since: null, hosts: [],
});
const mockedLayout = vi.mocked(api.getDashboardLayout);
const mockedSaveLayout = vi.mocked(api.saveDashboardLayout);

function renderRoute() {
  // D4-2.3：巡检报告动作长在顶栏（InspectionActionsControls 订阅总控台登记的控制器），
  // 所以路由级测试一律带上 Topbar，模拟 App.tsx 的真实外壳。
  return render(
    <MemoryRouter>
      <Topbar />
      <DashboardRoute refreshKey={0} />
    </MemoryRouter>
  );
}

/** C8-1：总控台只剩网格一条路径，等它出现 = 等首屏骨架让位。 */
function gridEl(container: HTMLElement) {
  return container.querySelector('.grid-stack');
}

/** 引擎节点里够用的最小面（坐标断言用），不为一个测试引进整套 gridstack 类型。 */
type TestNode = { id?: string | number; x?: number };

function gridNodes(container: HTMLElement): TestNode[] {
  const el = gridEl(container) as (HTMLElement & { gridstack?: { engine: { nodes: TestNode[] } } }) | null;
  return el?.gridstack?.engine.nodes ?? [];
}

function setViewport(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
}

beforeAll(async () => {
  // DashboardGrid 走 React.lazy：先把模块拉进缓存，免得动态 import 在全量并行下
  // 跟别的 worker 抢资源超过 waitFor 默认的 1s（D2 踩过这条）。
  await import('@/components/dashboard/dashboard-grid');
  // 12 列档：jsdom 默认 1024 属 6 列档，引擎会把存的 12 列坐标按 moveScale 折半，
  // 坐标断言就看不出「DB 里存的是什么」。存储契约本来也只认 12 列（见 edit-mode.tsx）。
  setViewport(1440);
});

afterAll(() => {
  setViewport(1024);
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("console.dashboard.view", "full");
  // D1'：布局默认无 override（空表 → 默认布局现算）；要测 override 的用例单独改写
  mockedLayout.mockResolvedValue({ items: [], updated_at: null });
  mockedMonitor.mockResolvedValue({
    systems: [{ id: '1', name: 'NAS', machine: 'nas', status: 'up' }],
    containers: [],
  });
  mockedMachines.mockResolvedValue([{ id: 'nas', label: 'NAS', role: 'server', actions: [] }]);
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
  mockedReport.mockResolvedValue('# 巡检报告\n\n## 总评\n');
  mockedErrorMessage.mockReturnValue('');
  mockedTailnet.mockResolvedValue({
    available: false,
    sampled_at: null,
    error: null,
    machines: [],
  });
});

test('总控台切页后首次挂载沿用缓存，当前挂载收到刷新事件才强刷', async () => {
  const view = (key: number) => (
    <MemoryRouter>
      <Topbar />
      <DashboardRoute refreshKey={key} />
    </MemoryRouter>
  );
  const { rerender } = render(view(3));
  await waitFor(() => expect(mockedMonitor).toHaveBeenCalled());
  expect(mockedMonitor).toHaveBeenLastCalledWith(false);

  rerender(view(4));
  await waitFor(() => expect(mockedMonitor).toHaveBeenCalledTimes(2));
  expect(mockedMonitor).toHaveBeenLastCalledWith(true);
});

test('总控台自动恢复读普通快照，手动刷新仍强刷', async () => {
  const view = (key: number, refreshForce: boolean) => (
    <MemoryRouter>
      <Topbar />
      <DashboardRoute refreshKey={key} refreshForce={refreshForce} />
    </MemoryRouter>
  );
  const { rerender } = render(view(0, true));
  await waitFor(() => expect(mockedMonitor).toHaveBeenCalledTimes(1));

  rerender(view(1, false));
  await waitFor(() => expect(mockedMonitor).toHaveBeenCalledTimes(2));
  expect(mockedMonitor).toHaveBeenLastCalledWith(false);
  expect(mockedObservability).toHaveBeenLastCalledWith(false);

  rerender(view(2, true));
  await waitFor(() => expect(mockedMonitor).toHaveBeenCalledTimes(3));
  expect(mockedMonitor).toHaveBeenLastCalledWith(true);
  expect(mockedObservability).toHaveBeenLastCalledWith(true);
});

test('默认简览不等慢监控，先显示已到的设备台账和入口', async () => {
  localStorage.removeItem('console.dashboard.view');
  mockedMonitor.mockReturnValue(new Promise(() => {}));
  renderRoute();
  expect(await screen.findByRole('region', { name: '设备简况' })).toBeTruthy();
  expect(screen.getAllByRole('heading', { name: '总控台' }).length).toBeGreaterThan(0);
});

test('default overview preserves hidden widgets and switching never writes DB layout', async () => {
  localStorage.removeItem('console.dashboard.view');
  mockedLayout.mockResolvedValue({
    items: [{ widget_key: 'ops-status', x: 0, y: 0, w: 6, hidden: true }],
    updated_at: null,
  });
  const { container } = renderRoute();
  await screen.findByRole('region', { name: '设备简况' });
  expect(container.querySelector('[data-dashboard-overview]')).toBeTruthy();
  expect(container.querySelector('.grid-stack')).toBeNull();
  expect(screen.queryByText('运行状态')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '完整总览' }));
  await waitFor(() => expect(gridEl(container)).toBeTruthy());
  expect(localStorage.getItem('console.dashboard.view')).toBe('full');
  fireEvent.click(screen.getByRole('button', { name: '简览' }));
  expect(container.querySelector('[data-dashboard-overview]')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '编辑布局' }));
  await waitFor(() => expect(gridEl(container)).toBeTruthy());
  fireEvent.click(screen.getByRole('button', { name: '放弃布局修改' }));
  expect(container.querySelector('[data-dashboard-overview]')).toBeTruthy();
  expect(mockedSaveLayout).not.toHaveBeenCalled();
});

describe('DashboardRoute observability 接线（D4）', () => {
  test('observability 失败不清空机器卡，也不白屏', async () => {
    mockedObservability.mockRejectedValue(new Error('boom'));
    const { container } = renderRoute();
    await waitFor(() => expect(screen.getByText('NAS')).toBeTruthy());
    expect(screen.queryByText('监控事件')).toBeNull();
    // 总控台其余区块仍渲染（C8-1 起是网格）
    expect(gridEl(container)).toBeTruthy();
  });

  test('observability 成功时摘要出现，机器卡与服务区不受影响', async () => {
    mockedObservability.mockResolvedValue({
      ok: true,
      updated_at: '2026-08-29T19:20:00Z',
      probe_interval: 600,
      source_errors: [],
      machine_links: {},
      active_alerts: [
        { machine: 'nas', name: 'Disk', severity: 'warn', threshold: 85, duration_min: 10, triggered_at: '2026-08-29T18:00:00Z' },
      ],
      recent_events: [],
      systemd_services: [],
      smart_devices: [],
    });
    renderRoute();
    await waitFor(() => expect(screen.getByText('监控事件')).toBeTruthy());
    // 事件名经 eventLabel 中文化（Disk → 磁盘使用率），原始名保留在 title
    expect(screen.getByText(/磁盘使用率/)).toBeTruthy();
    // 摘要里也有机器名（告警行），机器卡用多匹配断言仍在
    expect(screen.getAllByText('NAS').length).toBeGreaterThanOrEqual(1);
  });
});

describe('inspectionFileName', () => {
  test('用本地时间补零，扩展名固定 .md', () => {
    expect(inspectionFileName(new Date(2026, 8, 6, 9, 5))).toBe('巡检报告-2026-09-06-0905.md');
    expect(inspectionFileName(new Date(2026, 11, 31, 23, 59))).toBe('巡检报告-2026-12-31-2359.md');
  });
});

describe('DashboardRoute 一键巡检报告（C1，D4-2.3 起动作在顶栏）', () => {
  function copyButton() {
    return screen.getByRole('button', { name: '复制巡检报告' });
  }
  function downloadButton() {
    return screen.getByRole('button', { name: '下载巡检报告' });
  }

  test('两个动作都在顶栏且默认可点；正文区不再有「生成巡检报告」工具条', async () => {
    renderRoute();
    await waitFor(() => expect(copyButton()).toBeTruthy());
    expect((copyButton() as HTMLButtonElement).disabled).toBe(false);
    expect((downloadButton() as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText('生成巡检报告')).toBeNull();
  });

  test('复制：取 markdown 后写剪贴板并提示成功', async () => {
    renderRoute();
    await waitFor(() => expect(copyButton()).toBeTruthy());

    fireEvent.click(copyButton());

    await waitFor(() => expect(mockedCopy).toHaveBeenCalledWith('# 巡检报告\n\n## 总评\n'));
    expect(mockedReport).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledWith('巡检报告已复制到剪贴板');
  });

  test('下载：生成 blob 链接、带 .md 文件名，并在用完后 revoke', async () => {
    const createUrl = vi.fn(() => 'blob:report');
    const revokeUrl = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL: createUrl, revokeObjectURL: revokeUrl });
    const clicked: HTMLAnchorElement[] = [];
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        clicked.push(this);
      });

    try {
      renderRoute();
      await waitFor(() => expect(downloadButton()).toBeTruthy());

      fireEvent.click(downloadButton());

      await waitFor(() => expect(clicked.length).toBe(1));
      expect(clicked[0].href).toContain('blob:report');
      expect(clicked[0].download).toMatch(/^巡检报告-\d{4}-\d{2}-\d{2}-\d{4}\.md$/);
      expect(revokeUrl).toHaveBeenCalledWith('blob:report');
      // 下载路径不碰剪贴板
      expect(mockedCopy).not.toHaveBeenCalled();
      expect(toast.success).toHaveBeenCalledWith('巡检报告已下载');
      // 临时 anchor 不留在 DOM 里
      expect(document.querySelectorAll('a[download]').length).toBe(0);
    } finally {
      clickSpy.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  test('生成期间两个按钮都禁用，结束后恢复', async () => {
    let release: (value: string) => void = () => {};
    mockedReport.mockReturnValue(
      new Promise<string>((resolve) => {
        release = resolve;
      })
    );
    renderRoute();
    await waitFor(() => expect(copyButton()).toBeTruthy());

    fireEvent.click(copyButton());

    await waitFor(() => expect((copyButton() as HTMLButtonElement).disabled).toBe(true));
    expect((downloadButton() as HTMLButtonElement).disabled).toBe(true);

    release('# 巡检报告\n');
    await waitFor(() => expect((copyButton() as HTMLButtonElement).disabled).toBe(false));
  });

  test('失败：报后端错误文案，按钮恢复可点，不写剪贴板', async () => {
    mockedReport.mockRejectedValue(new Error('boom'));
    mockedErrorMessage.mockReturnValue('format 只支持 json 或 markdown');
    renderRoute();
    await waitFor(() => expect(copyButton()).toBeTruthy());

    fireEvent.click(copyButton());

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('format 只支持 json 或 markdown')
    );
    expect(mockedCopy).not.toHaveBeenCalled();
    expect((copyButton() as HTMLButtonElement).disabled).toBe(false);
  });

  test('后端没给错误文案时退回兜底文案', async () => {
    mockedReport.mockRejectedValue(new Error('boom'));
    mockedErrorMessage.mockReturnValue('');
    renderRoute();
    await waitFor(() => expect(copyButton()).toBeTruthy());

    fireEvent.click(copyButton());

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('生成巡检报告失败'));
  });
});

describe('C8-1 唯一渲染路径（静态渲染树已删除）', () => {
  test('DB 无 override 也走网格：默认布局现算，分区标题按裁决丢失', async () => {
    const { container } = renderRoute();
    await waitFor(() => expect(gridEl(container)).toBeTruthy());
    await waitFor(() => expect(screen.getByText('NAS')).toBeTruthy());
    // 评审已裁决：分区标题「服务器/节点」与「设备与服务」链接明确接受丢失
    expect(screen.queryByText('服务器')).toBeNull();
    expect(screen.queryByText('节点')).toBeNull();
    expect(screen.queryByText('设备与服务')).toBeNull();
  });

  test('DB 有 override 时按 DB 坐标铺格', async () => {
    mockedLayout.mockResolvedValue({
      items: [{ widget_key: 'machine-card:nas', x: 6, y: 0, w: 3, hidden: false }],
      updated_at: '2026-09-06T00:00:00Z',
    });
    const { container } = renderRoute();
    await waitFor(() => expect(gridEl(container)).toBeTruthy());
    await waitFor(() =>
      expect(gridNodes(container).find((n) => n.id === 'machine-card:nas')?.x).toBe(6)
    );
  });

  test('首屏三份数据未到位时是骨架，不是空白也不是半截网格', async () => {
    // 布局 GET 悬着 → 停在骨架；机器卡骨架按兜底 4 列铺（台账也还没到）
    mockedLayout.mockReturnValue(new Promise(() => {}));
    const { container } = renderRoute();
    const skeleton = await screen.findByRole('status', { name: '总控台加载中' });
    expect(skeleton.querySelectorAll('.animate-pulse').length).toBeGreaterThanOrEqual(4);
    expect(gridEl(container)).toBeNull();
  });
});

describe("C8 / D1' 布局加载与一次性迁移", () => {
  test('GET 失败 → 只读降级：按默认布局现算渲染并明确报错，不白屏、编辑入口不登记', async () => {
    mockedLayout.mockRejectedValue(new Error('boom'));
    const { container } = renderRoute();
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    // 降级态仍出网格（buildDefaultLayout 现算），不是白屏也不是卡在骨架上
    await waitFor(() => expect(gridEl(container)).toBeTruthy());
    expect(screen.getByText('NAS')).toBeTruthy();
    expect(screen.queryByRole('status', { name: '总控台加载中' })).toBeNull();
    expect(screen.queryByRole('button', { name: '编辑布局' })).toBeNull();
  });

  test('GET 失败时 hidden 一并降级为全显示', async () => {
    // 失败路径拿不到 hidden 集合，渲染必须回到「默认布局全显示」而不是缺角
    mockedLayout.mockRejectedValue(new Error('boom'));
    const { container } = renderRoute();
    await waitFor(() => expect(gridEl(container)).toBeTruthy());
    const keys = gridNodes(container).map((n) => n.id);
    // 默认 6 件：今日关注 / 监控事件 / NAS / 运维状态 / tailnet / 入口流量
    expect(keys).toContain('tailnet');
    expect(keys).toContain('caddy-entry');
    expect(keys).toContain('machine-card:nas');
  });

  test('legacy localStorage → 一次性迁移：PUT 到 DB 成功后清旧键，页面按 legacy 显示', async () => {
    localStorage.setItem(
      'console.dashboard.layout',
      JSON.stringify([{ key: 'machine-card:nas', x: 6, y: 0, w: 3 }])
    );
    const { container } = renderRoute();
    // legacy override 被搬进 DB，页面随即按 legacy 坐标显示
    await waitFor(() => expect(mockedSaveLayout).toHaveBeenCalled());
    const payload = mockedSaveLayout.mock.calls[0][0];
    expect(payload).toEqual([
      { widget_key: 'machine-card:nas', x: 6, y: 0, w: 3, hidden: false },
    ]);
    await waitFor(() =>
      expect(localStorage.getItem('console.dashboard.layout')).toBeNull()
    );
    await waitFor(() =>
      expect(gridNodes(container).find((n) => n.id === 'machine-card:nas')?.x).toBe(6)
    );
  });

  test('legacy 与 DB 内容一致 → 不做无谓 PUT，直接退役旧键', async () => {
    localStorage.setItem(
      'console.dashboard.layout',
      JSON.stringify([{ key: 'machine-card:nas', x: 6, y: 0, w: 3 }])
    );
    mockedLayout.mockResolvedValue({
      items: [{ widget_key: 'machine-card:nas', x: 6, y: 0, w: 3, hidden: false }],
      updated_at: '2026-09-06T00:00:00Z',
    });
    renderRoute();
    await waitFor(() => expect(screen.getByText('NAS')).toBeTruthy());
    // 给迁移 effect 留出执行窗口
    await waitFor(() => expect(localStorage.getItem('console.dashboard.layout')).toBeNull());
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });

  test('迁移 PUT 失败 → 旧键保留待重试，本次仍按 legacy 显示', async () => {
    localStorage.setItem(
      'console.dashboard.hidden',
      JSON.stringify(['tailnet'])
    );
    mockedSaveLayout.mockRejectedValue(new Error('boom'));
    renderRoute();
    await waitFor(() => expect(mockedSaveLayout).toHaveBeenCalled());
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(localStorage.getItem('console.dashboard.hidden')).toBeTruthy();
  });
});
