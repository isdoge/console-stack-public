import { beforeEach, describe, expect, test, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { DevicesRoute, etaText, trendTone } from './devices';
import { Topbar } from '@/components/layout/topbar';
import type { DiskTrend, MonitorSystem } from '@/lib/types';
import * as api from '@/lib/api';
import { toast } from 'sonner';

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}));

vi.mock('@/lib/api', () => ({
  getCapacity: vi.fn(),
  getMonitorHistory: vi.fn(),
  getConfig: vi.fn(),
  getMonitor: vi.fn(),
  getObservability: vi.fn(),
  listMachines: vi.fn(),
  getServicesSnapshot: vi.fn(),
  runServiceAction: vi.fn(),
  setServicesPriority: vi.fn(),
}));

const contributedNavigation = vi.hoisted(() => ({ groups: [] as import('@/lib/plugins/navigation-types').NavigationContribution[] }));
vi.mock('@/lib/plugins/navigation', () => ({ usePluginNavigation: () => contributedNavigation.groups }));

const mockedCapacity = vi.mocked(api.getCapacity);
const mockedConfig = vi.mocked(api.getConfig);
const mockedMonitor = vi.mocked(api.getMonitor);
const mockedObservability = vi.mocked(api.getObservability);
const mockedMachines = vi.mocked(api.listMachines);
const mockedServices = vi.mocked(api.getServicesSnapshot);

/** 线上实测 NAS 的那组数：涨得慢 + 拟合差 → 有增长率但没有耗尽日期。 */
const TREND: DiskTrend = {
  samples: 90,
  span_hours: 720,
  first_at: '2026-08-08T00:00:00Z',
  last_at: '2026-09-07T00:00:00Z',
  disk_total_gb: 412.8,
  disk_used_gb: 40.2,
  disk_free_gb: 372.6,
  daily_gb: 0.3,
  r_squared: 0.238,
  trend: 'rising',
  days_to_full: null,
};

function renderRoute(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Topbar />
      <Routes>
        <Route path="/devices" element={<DevicesRoute refreshKey={0} />} />
        <Route path="/devices/:id" element={<DevicesRoute refreshKey={0} />} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  contributedNavigation.groups = [];
  localStorage.removeItem('console.device.blocks');
  mockedConfig.mockResolvedValue({ probe_interval: 600 });
  vi.mocked(api.getMonitorHistory).mockResolvedValue({
    ok: true, machine: 'nas', source: 'console_pg', resolution: 'raw',
    window: { start: '2026-10-26T00:00:00Z', end: '2026-10-26T01:00:00Z', effective_end: '2026-10-26T01:00:00Z' },
    series: [{ metric: 'cpu', unit: 'percent', interval_sec: 300, gaps: [], points: [
      { ts: '2026-10-26T00:00:00Z', value: 11 }, { ts: '2026-10-26T00:05:00Z', value: 12 },
    ] }], events: [],
  });
  // M2 默认「没有趋势」：样本不足的机器本就不出现在 machines 里，磁盘趋势卡整块不渲染
  mockedCapacity.mockResolvedValue({
    ok: true,
    ttl: 3600,
    history_type: '480m',
    min_r_squared: 0.5,
    machines: {},
    error: null,
  });
  mockedObservability.mockResolvedValue({
    ok: true, updated_at: null, probe_interval: 600, source_errors: [], machine_links: {},
    active_alerts: [], recent_events: [], systemd_services: [], smart_devices: [],
  });
  mockedMonitor.mockResolvedValue({
    systems: [{ id: '1', name: 'NAS', machine: 'nas', status: 'up' }],
    containers: [],
  });
  mockedMachines.mockResolvedValue([{ id: 'nas', label: 'NAS', role: 'server', actions: [], probe_status: 'up' }]);
  mockedServices.mockResolvedValue({
    services: [{ name: 'console-brain', machine: 'nas', running: 'running', actions: [] }],
    snapshot: { sampledAt: null, refreshing: false, error: false },
  });
});

test('通用导航按机器展示并进入服务上下文，外链打开方式保留', async () => {
  contributedNavigation.groups = [{ owner: 'sample', label: '合成入口', commandGroup: '工具', emptyMessage: '无匹配', links: [
    { id: 'one', label: '合成工具', to: 'https://example.test', external: true, machine: 'nas', serviceNames: ['console-brain'] },
    { id: 'other', label: '另一台工具', to: '/sample/two', machine: 'other' },
  ] }];
  renderRoute('/devices/nas');
  const target = await screen.findByRole('link', { name: '合成工具' });
  expect(target.getAttribute('target')).toBe('_blank');
  expect(target.getAttribute('href')).toBe('https://example.test');
  expect(screen.queryByRole('link', { name: '另一台工具' })).toBeNull();
  fireEvent.click(await screen.findByText('console-brain'));
  const dialog = await screen.findByRole('dialog', { name: 'console-brain · 服务上下文' });
  const serviceTarget = within(dialog).getByRole('link', { name: '合成工具' });
  expect(serviceTarget).not.toBe(target);
  expect(serviceTarget.getAttribute('href')).toBe('https://example.test');
  expect(serviceTarget.getAttribute('target')).toBe('_blank');
  expect(serviceTarget.getAttribute('rel')).toBe('noopener noreferrer');
  expect(within(dialog).queryByRole('link', { name: '另一台工具' })).toBeNull();
});

test('旧panels隐藏偏好沿用到通用导航块', async () => {
  localStorage.setItem('console.device.blocks', JSON.stringify({ panels: false }));
  contributedNavigation.groups = [{ owner: 'sample', label: '合成入口', commandGroup: '工具', emptyMessage: '无匹配',
    links: [{ id: 'one', label: '合成工具', to: '/sample/one', machine: 'nas' }] }];
  renderRoute('/devices/nas');
  await screen.findByText('console-brain');
  expect(screen.queryByRole('link', { name: '合成工具' })).toBeNull();
});

test('单机路由接入真实历史查询、时间范围和同窗事件', async () => {
  mockedMonitor.mockResolvedValue({ source: 'console_pg', systems: [{ id: '1', name: 'NAS', machine: 'nas', status: 'up' }], containers: [] });
  vi.mocked(api.getMonitorHistory).mockResolvedValueOnce({
    ok: true, machine: 'nas', source: 'console_pg', resolution: 'raw',
    window: { start: '2026-10-26T00:00:00Z', end: '2026-10-26T01:00:00Z', effective_end: '2026-10-26T01:00:00Z' },
    series: [{ metric: 'cpu', unit: 'percent', interval_sec: 300, gaps: [], points: [{ ts: '2026-10-26T00:00:00Z', value: 11 }] }],
    events: [{ id: 801, fingerprint: 'machine:nas', kind: 'machine', source: 'probe', target: 'nas', machine: 'nas',
      severity: 'error', title: '同窗机器离线', detail: '', opened_at: '2026-10-25T23:50:00Z',
      updated_at: '2026-10-26T00:05:00Z', resolved_at: '2026-10-26T00:05:00Z', state: 'resolved', duration_sec: 900 }],
  });
  renderRoute('/devices/nas');
  await screen.findByLabelText(/CPU历史曲线/);
  expect(document.querySelector('[data-event-bar="801"]')?.getAttribute('title')).toContain('同窗机器离线');
  expect(api.getMonitorHistory).toHaveBeenCalledWith(expect.objectContaining({ machine: 'nas', metrics: ['cpu', 'mem_pct', 'disk_pct', 'load1'] }));
  const first = vi.mocked(api.getMonitorHistory).mock.calls[0][0];
  expect(Date.parse(first.end) - Date.parse(first.start)).toBe(24 * 3600_000);
  fireEvent.click(screen.getByRole('button', { name: '7 天' }));
  await waitFor(() => expect(api.getMonitorHistory).toHaveBeenCalledTimes(2));
  const second = vi.mocked(api.getMonitorHistory).mock.calls[1][0];
  expect(Date.parse(second.end) - Date.parse(second.start)).toBe(7 * 24 * 3600_000);
});

test('切换时间窗后忽略晚到的旧请求', async () => {
  mockedMonitor.mockResolvedValue({ source: 'console_pg', systems: [], containers: [] });
  let finishOld!: (value: Awaited<ReturnType<typeof api.getMonitorHistory>>) => void;
  vi.mocked(api.getMonitorHistory).mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }));
  renderRoute('/devices/nas');
  await waitFor(() => expect(api.getMonitorHistory).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole('button', { name: '7 天' }));
  await screen.findByLabelText(/CPU历史曲线/);
  await act(async () => finishOld({ ok: true, machine: 'nas', source: 'console_pg', resolution: 'raw',
    window: { start: '2026-10-26T00:00:00Z', end: '2026-10-26T01:00:00Z', effective_end: '2026-10-26T01:00:00Z' },
    series: [{ metric: 'load15', points: [{ ts: '2026-10-26T00:00:00Z', value: 99 }], gaps: [], unit: 'count', interval_sec: 300 }], events: [] }));
  expect(screen.queryByLabelText(/负载 15m历史曲线/)).toBeNull();
  expect(screen.getByRole('button', { name: '7 天' }).getAttribute('aria-pressed')).toBe('true');
});

test('历史失败不影响服务列表，且可重试', async () => {
  mockedMonitor.mockResolvedValue({ source: 'console_pg', systems: [], containers: [] });
  vi.mocked(api.getMonitorHistory).mockRejectedValueOnce(new Error('unavailable'));
  renderRoute('/devices/nas');
  await screen.findByText('历史数据暂时无法读取，请稍后重试。');
  expect(screen.getByText('console-brain')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '重试历史查询' }));
  await screen.findByLabelText(/CPU历史曲线/);
});

test('服务名称打开增强上下文，动作确认取消不调用后端', async () => {
  mockedServices.mockResolvedValue({ services: [{ name: 'console-brain', machine: 'nas', type: 'docker', running: 'running', actions: ['restart'] }],
    snapshot: { sampledAt: null, refreshing: false, error: false } });
  renderRoute('/devices/nas');
  fireEvent.click(await screen.findByText('console-brain'));
  await screen.findByText('服务详情与健康');
  expect(screen.getByText('配置摘要')).toBeTruthy();
  expect(screen.getByText('依赖关系与入口链路')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '重启' }));
  await screen.findByRole('alertdialog');
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(api.runServiceAction).not.toHaveBeenCalled();
});

test('设备动作属于顶栏，新增打开草稿且离开路由后撤销动作', async () => {
  mockedObservability.mockResolvedValue({
    ok: true, updated_at: null, probe_interval: 600, source_errors: [], machine_links: {},
    active_alerts: [], recent_events: [], systemd_services: [], smart_devices: [],
  });
  render(
    <MemoryRouter initialEntries={['/devices/nas']}>
      <Topbar />
      <Link to="/settings">离开设备页</Link>
      <Routes>
        <Route path="/devices/:id" element={<DevicesRoute refreshKey={0} />} />
        <Route path="/settings" element={<div>设置页</div>} />
      </Routes>
    </MemoryRouter>
  );
  const edit = await screen.findByRole('button', { name: '编辑本机' });
  expect(edit.closest('header')).not.toBeNull();
  const add = screen.getByRole('button', { name: '机器' });
  expect(add.closest('header')).not.toBeNull();
  fireEvent.click(add);
  expect(await screen.findByRole('dialog')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '关闭侧栏' }));
  fireEvent.click(screen.getByRole('link', { name: '离开设备页' }));
  await screen.findByText('设置页');
  expect(screen.queryByRole('button', { name: '机器' })).toBeNull();
  expect(screen.queryByRole('button', { name: '编辑本机' })).toBeNull();
});

test('切页继承旧刷新序号不强刷；仅当前页收到新序号才强刷', async () => {
  mockedObservability.mockResolvedValue({
    ok: true, updated_at: null, probe_interval: 600, source_errors: [], machine_links: {},
    active_alerts: [], recent_events: [], systemd_services: [], smart_devices: [],
  });
  const view = (key: number) => (
    <MemoryRouter>
      <DevicesRoute refreshKey={key} />
    </MemoryRouter>
  );
  const { rerender } = render(view(3));
  await waitFor(() => expect(mockedMonitor).toHaveBeenCalled());
  expect(mockedMonitor).toHaveBeenLastCalledWith(false);
  expect(mockedServices).toHaveBeenLastCalledWith(false);

  rerender(view(4));
  await waitFor(() => expect(mockedMonitor).toHaveBeenCalledTimes(2));
  expect(mockedMonitor).toHaveBeenLastCalledWith(true);
  expect(mockedServices).toHaveBeenLastCalledWith(true);
});

test('自动恢复不以强刷请求覆盖设备页首屏快照', async () => {
  const view = (key: number, refreshForce: boolean) => (
    <MemoryRouter>
      <DevicesRoute refreshKey={key} refreshForce={refreshForce} />
    </MemoryRouter>
  );
  const { rerender } = render(view(0, true));
  await waitFor(() => expect(mockedServices).toHaveBeenCalledTimes(1));

  rerender(view(1, false));
  await waitFor(() => expect(mockedServices).toHaveBeenCalledTimes(2));
  expect(mockedServices).toHaveBeenLastCalledWith(false);
  expect(mockedMonitor).toHaveBeenLastCalledWith(false);

  rerender(view(2, true));
  await waitFor(() => expect(mockedServices).toHaveBeenCalledTimes(3));
  expect(mockedServices).toHaveBeenLastCalledWith(true);
  expect(mockedMonitor).toHaveBeenLastCalledWith(true);
});

test.each([undefined, '升级成功，但版本刷新失败，请在版本详情中重试刷新'])(
  '升级完成后刷新版本行，并保留版本刷新失败提示：%s',
  async (warning) => {
    const svc = {
      name: 'magpie', machine: 'nas', running: 'running', actions: ['upgrade' as const],
      compose_dir: '/opt/magpie',
    };
    mockedMachines.mockResolvedValue([{
      id: 'nas', label: 'NAS', role: 'server', upgrade: { enabled: true },
    }]);
    mockedServices.mockResolvedValueOnce({
      services: [{ ...svc, version_info: { local: 'v0.1.1311', upstream: 'v0.1.1326' } }],
      snapshot: { sampledAt: null, refreshing: false, error: false },
    }).mockResolvedValue({
      services: [{
        ...svc,
        version_info: { local: 'v0.1.1326', upstream: 'v0.1.1326', status: '最新' },
      }],
      snapshot: { sampledAt: null, refreshing: false, error: false },
    });
    vi.mocked(api.runServiceAction).mockResolvedValue({ ok: true, warning });
    renderRoute('/devices/nas');
    await screen.findByText('0.1.1311');
    fireEvent.keyDown(screen.getByRole('button', { name: '操作' }), { key: 'ArrowDown' });
    fireEvent.click(await screen.findByRole('menuitem', { name: '升级' }));
    await waitFor(() => expect(api.runServiceAction).toHaveBeenCalledWith('magpie', 'upgrade'));
    await waitFor(() => expect(mockedServices).toHaveBeenLastCalledWith(true));
    await waitFor(() => expect(screen.queryByText('0.1.1311')).toBeNull());
    expect(screen.getAllByText('0.1.1326').length).toBeGreaterThan(0);
    if (warning) {
      expect(toast.warning).toHaveBeenCalledWith(warning);
      expect(toast.success).not.toHaveBeenCalled();
    } else {
      expect(toast.success).toHaveBeenCalledWith('升级成功');
    }
    expect(toast.error).not.toHaveBeenCalled();
  }
);

describe('DevicesRoute observability 接线（D4）', () => {
  test('observability 失败不清空本机服务表，也不白屏', async () => {
    mockedObservability.mockRejectedValue(new Error('boom'));
    renderRoute('/devices/nas');
    await waitFor(() => expect(screen.getByText('console-brain')).toBeTruthy());
    // 扩展区不渲染，服务表与机器卡保留
    expect(screen.queryByText('本机事件')).toBeNull();
    expect(screen.getAllByText('NAS').length).toBeGreaterThanOrEqual(2);
  });

  test('observability 成功时本机事件与 systemd 出现', async () => {
    mockedObservability.mockResolvedValue({
      ok: true,
      updated_at: '2026-08-30T00:00:00Z',
      probe_interval: 600,
      source_errors: [],
      machine_links: {},
      active_alerts: [],
      recent_events: [
        { machine: 'nas', name: 'Disk', severity: 'warn', threshold: 85, opened_at: '2026-08-29T10:00:00Z', resolved_at: null, state: 'active', duration_sec: null },
      ],
      systemd_services: [
        { machine: 'nas', unit: 'docker.service', state: 'active', sub_state: 'running', cpu_pct: 0, cpu_peak_pct: 0, memory_bytes: 1, memory_peak_bytes: 1, updated_at: '2026-08-29T19:16:21Z' },
      ],
      smart_devices: [],
    });
    renderRoute('/devices/nas');
    await waitFor(() => expect(screen.getByText('本机事件')).toBeTruthy());
    // 事件名中文化：Disk → 磁盘使用率
    expect(screen.getByText(/磁盘使用率/)).toBeTruthy();
    expect(screen.getByText('docker.service')).toBeTruthy();
    expect(screen.getByText('console-brain')).toBeTruthy();
  });
});

describe('trendTone / etaText（M2 纯函数）', () => {
  test('三种趋势各有中文与色调，flat 不上色', () => {
    expect(trendTone('rising')).toEqual({ label: '在增长', cls: 'text-amber-700' });
    expect(trendTone('falling')).toEqual({ label: '在减少', cls: 'text-emerald-600' });
    expect(trendTone('flat').cls).toBe('text-muted-foreground');
    // 后端加了新枚举也不能白屏
    expect(trendTone('unknown').label).toBe('基本不动');
  });

  test('有 days_to_full 才给天数', () => {
    expect(etaText({ ...TREND, days_to_full: 12.5 }, 0.5)).toBe('约 12.5 天后写满');
  });

  test('拟合差时说明原因；其余两种成因不猜（后端刻意不区分）', () => {
    expect(etaText({ ...TREND, r_squared: 0.238 }, 0.5)).toBe('暂不预测（趋势不稳定）');
    expect(etaText({ ...TREND, r_squared: 0.863 }, 0.5)).toBe('暂不预测');
    expect(etaText({ ...TREND, r_squared: null }, 0.5)).toBe('暂不预测');
  });

  test('阈值随后端下发；拿不到阈值时不解释原因，而不是猜一个 0.5', () => {
    // 后端把线挪到 0.2 → 同一个 0.238 就不再算「不稳定」
    expect(etaText({ ...TREND, r_squared: 0.238 }, 0.2)).toBe('暂不预测');
    expect(etaText({ ...TREND, r_squared: 0.238 }, null)).toBe('暂不预测');
  });
});

describe('DevicesRoute 磁盘趋势接线（M2）', () => {
  test('本机有趋势时渲染磁盘趋势卡', async () => {
    mockedObservability.mockRejectedValue(new Error('boom'));
    mockedCapacity.mockResolvedValue({
      ok: true, ttl: 3600, history_type: '480m', min_r_squared: 0.5, error: null,
      machines: { nas: TREND },
    });
    renderRoute('/devices/nas');
    await waitFor(() => expect(screen.getByText('磁盘趋势')).toBeTruthy());
    expect(screen.getByText('+0.3 GB/天')).toBeTruthy();
    // 线上实测 NAS 就是「拟合差 → 不给耗尽日期」这一档
    expect(screen.getByText('暂不预测（趋势不稳定）')).toBeTruthy();
  });

  test('本机没有趋势（样本不足）→ 整卡不渲染，不给空占位', async () => {
    mockedObservability.mockRejectedValue(new Error('boom'));
    renderRoute('/devices/nas');
    await waitFor(() => expect(screen.getByText('console-brain')).toBeTruthy());
    expect(screen.queryByText('磁盘趋势')).toBeNull();
  });

  test('取数失败不影响服务表', async () => {
    mockedObservability.mockRejectedValue(new Error('boom'));
    mockedCapacity.mockRejectedValue(new Error('beszel down'));
    renderRoute('/devices/nas');
    await waitFor(() => expect(screen.getByText('console-brain')).toBeTruthy());
    expect(screen.queryByText('磁盘趋势')).toBeNull();
  });

  test('node 单机视图不拉磁盘趋势（后端只对 server 算）', async () => {
    mockedObservability.mockRejectedValue(new Error('boom'));
    mockedMachines.mockResolvedValue([
      { id: 'nas', label: 'NAS', role: 'server', actions: [], probe_status: 'up' },
      { id: 'kr', label: '光帆 KR', role: 'node', actions: [], probe_status: 'up' },
    ]);
    renderRoute('/devices/kr');
    await waitFor(() => expect(screen.getAllByText('光帆 KR').length).toBeGreaterThanOrEqual(2));
    expect(mockedCapacity).not.toHaveBeenCalled();
  });
});

describe('B3-1 单机页四分区与能力裁剪', () => {
  const emptyObservability = {
    ok: true, updated_at: null, probe_interval: 600, source_errors: [], machine_links: {},
    active_alerts: [], recent_events: [], systemd_services: [], smart_devices: [],
  };

  test('单机视图按概览/服务/可用性/事件分区；概览区复用 B2-2 概况卡；server 也开放探测历史', async () => {
    mockedObservability.mockResolvedValue(emptyObservability);
    mockedMachines.mockResolvedValue([{
      id: 'nas', label: 'NAS', role: 'server', capabilities: ['docker', 'beszel', 'ssh'],
      actions: [], probe_status: 'up',
      probe_history: [{ ts: '08:00', status: 'up', latency_ms: 12 }],
    }]);
    renderRoute('/devices/nas');
    await waitFor(() => expect(screen.getByText('console-brain')).toBeTruthy());
    for (const section of ['overview', 'services', 'availability', 'events']) {
      expect(document.querySelector(`[data-section="${section}"]`)).not.toBeNull();
    }
    // B2-2 设备概况卡在概览区（B3 复用，不重复造卡）
    expect(document.querySelector('[data-machine-overview-card="nas"]')).not.toBeNull();
    expect(screen.getByText('探测历史')).toBeTruthy();
    expect(screen.getByRole('button', { name: '扫描未登记容器' })).toBeTruthy();
  });

  test('无 docker 的 node 保留已登记进程服务及添加入口', async () => {
    mockedObservability.mockResolvedValue(emptyObservability);
    mockedMachines.mockResolvedValue([{
      id: 'kr', label: '光帆 KR', role: 'node', capabilities: ['beszel', 'ssh'],
      actions: [], probe_status: 'up',
    }]);
    mockedServices.mockResolvedValue({
      services: [{ name: 'sing-box-kr', machine: 'kr', type: 'process', running: 'running', actions: [] }],
      snapshot: { sampledAt: null, refreshing: false, error: false },
    });
    renderRoute('/devices/kr');
    await screen.findByText('sing-box-kr');
    expect(document.querySelector('[data-section="services"]')).not.toBeNull();
    expect(screen.getByRole('button', { name: '添加服务' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '扫描未登记容器' })).toBeNull();
    // 概览区仍在：无 monitor 快照时概况卡按「暂无数据」表达，不画假读数
    expect(document.querySelector('[data-section="overview"]')).not.toBeNull();
  });

  test('快照容器能力优先于台账：隐藏容器数据和扫描，保留服务管理', async () => {
    mockedObservability.mockResolvedValue(emptyObservability);
    mockedMachines.mockResolvedValue([{
      id: 'nas', label: 'NAS', role: 'server', capabilities: ['docker', 'beszel', 'ssh'],
      actions: [], probe_status: 'up',
    }]);
    // B1-1 §4.1：systems[] 是 MonitorSystem 的超集扩展；capabilities 字段
    // 由 B1 后端实施后进入 TS 类型，这里模拟契约下发形态
    const systemWithCaps = {
      id: '1', name: 'NAS', machine: 'nas', status: 'up',
      capabilities: { containers: { status: 'unsupported' as const } },
    };
    mockedMonitor.mockResolvedValue({
      systems: [systemWithCaps as unknown as MonitorSystem],
      containers: [{ system_id: '1', machine: 'nas', name: 'stale-container' }],
    });
    renderRoute('/devices/nas');
    await screen.findByText('console-brain');
    expect(document.querySelector('[data-section="services"]')).not.toBeNull();
    expect(screen.queryByText('stale-container')).toBeNull();
    expect(screen.queryByRole('button', { name: '扫描未登记容器' })).toBeNull();
  });

  test('GZ 仅有 SSH 仍显示 Magpie，设置能打开 systemd 服务配置', async () => {
    mockedMachines.mockResolvedValue([{
      id: 'gz', label: 'GZ', role: 'server', capabilities: ['ssh'],
    }]);
    mockedMonitor.mockResolvedValue({ systems: [], containers: [
      { system_id: 'gz', machine: 'gz', name: 'unexpected-container' },
    ] });
    mockedServices.mockResolvedValue({
      services: [{ name: 'gz-magpie', machine: 'gz', type: 'process', systemd_unit: 'magpie.service',
        running: 'running', actions: ['start', 'stop', 'restart', 'logs'] }],
      snapshot: { sampledAt: null, refreshing: false, error: false },
    });
    renderRoute('/devices/gz');
    await screen.findByText('gz-magpie');
    expect(screen.queryByText('unexpected-container')).toBeNull();
    expect(screen.queryByRole('button', { name: '扫描未登记容器' })).toBeNull();
    fireEvent.keyDown(screen.getByRole('button', { name: '操作' }), { key: 'ArrowDown' });
    fireEvent.click(await screen.findByRole('menuitem', { name: '设置' }));
    await screen.findByRole('dialog');
    expect(screen.getByText('编辑服务 · gz-magpie')).toBeTruthy();
    expect((screen.getByLabelText('systemd 服务单元') as HTMLInputElement).value).toBe('magpie.service');
    expect(api.runServiceAction).not.toHaveBeenCalled();
  });

  test('GZ 没有登记服务时仍可添加，默认选中 GZ', async () => {
    mockedMachines.mockResolvedValue([{
      id: 'gz', label: 'GZ', role: 'server', capabilities: ['ssh'],
    }]);
    mockedMonitor.mockResolvedValue({ systems: [], containers: [] });
    mockedServices.mockResolvedValue({
      services: [], snapshot: { sampledAt: null, refreshing: false, error: false },
    });
    renderRoute('/devices/gz');
    fireEvent.click(await screen.findByRole('button', { name: '添加服务' }));
    const dialog = await screen.findByRole('dialog');
    expect((dialog.querySelector('select') as HTMLSelectElement).value).toBe('gz');
    expect(screen.queryByRole('button', { name: '扫描未登记容器' })).toBeNull();
  });
});

describe('DevicesRoute 机器对比入口（M3）', () => {
  test('机器不足两台时不给对比入口', async () => {
    mockedObservability.mockRejectedValue(new Error('boom'));
    renderRoute('/devices');
    await waitFor(() => expect(screen.getByText('console-brain')).toBeTruthy());
    expect(screen.queryByRole('button', { name: '对比' })).toBeNull();
  });

  test('两台以上时点开对比框，此时才拉磁盘趋势', async () => {
    mockedObservability.mockRejectedValue(new Error('boom'));
    mockedMachines.mockResolvedValue([
      { id: 'nas', label: 'NAS', role: 'server', actions: [], probe_status: 'up' },
      { id: 'kr', label: '光帆 KR', role: 'node', actions: [], probe_status: 'up' },
    ]);
    renderRoute('/devices');
    const button = await screen.findByRole('button', { name: '对比' });
    expect(mockedCapacity).not.toHaveBeenCalled();
    fireEvent.click(button);
    expect(await screen.findByText('机器对比')).toBeTruthy();
    await waitFor(() => expect(mockedCapacity).toHaveBeenCalled());
  });
});
