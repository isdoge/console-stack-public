import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from './App';
import { setDashboardEditController } from '@/components/dashboard/edit-mode';
import type { DashboardEditController } from '@/components/dashboard/edit-mode';

// A6：PWA 恢复刷新只关心 Shell 派发给默认路由的 refreshKey，
// mock 掉总控台真实数据请求/GridStack，避免整套后端 fetch 依赖进本文件。
vi.mock('@/routes/dashboard', () => ({
  DashboardRoute: ({ refreshKey, refreshForce }: { refreshKey: number; refreshForce?: boolean }) => (
    <div data-testid="dashboard-refresh-key" data-refresh-force={String(refreshForce)}>{refreshKey}</div>
  ),
}));

function controllerOf(editing: boolean): DashboardEditController {
  return {
    editing,
    enter: vi.fn(),
    save: vi.fn(),
    cancel: vi.fn(),
    saving: false,
    reset: vi.fn(),
    hidden: [],
    show: vi.fn(),
  };
}

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', {
    value: state,
    configurable: true,
  });
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
}

function firePageShow(persisted: boolean) {
  const event = new Event('pageshow');
  Object.defineProperty(event, 'persisted', { value: persisted, configurable: true });
  act(() => {
    window.dispatchEvent(event);
  });
}

afterEach(() => {
  // 与 topbar.test.tsx 同口径：撤销模块级编辑态登记，避免跨用例泄漏
  setDashboardEditController(null);
  Object.defineProperty(document, 'visibilityState', {
    value: 'visible',
    configurable: true,
  });
});

test('renders shell with sidebar navigation', () => {
  const { container } = render(<App />);
  // 侧栏导航渲染（Sidebar 的 <nav> 结构断言，防止 Shell 布局静默崩坏）
  expect(container.querySelector('nav')).not.toBeNull();
  expect(container.querySelector('aside')).not.toBeNull();
  // 侧栏折叠按钮可访问（aria-label 断言，同时覆盖可访问性契约）
  expect(screen.getByLabelText(/侧栏/)).toBeTruthy();
});

test('恢复可见时刷新一次，隐藏态不刷新（A6）', () => {
  render(<App />);
  const key = () => screen.getByTestId('dashboard-refresh-key').textContent;
  expect(key()).toBe('0');

  setVisibility('hidden');
  expect(key()).toBe('0');

  setVisibility('visible');
  expect(key()).toBe('1');
});

test('自动恢复走普通快照，手动刷新才强刷', async () => {
  render(<App />);
  const view = () => screen.getByTestId('dashboard-refresh-key');

  setVisibility('visible');
  expect(view().dataset.refreshForce).toBe('false');

  await waitFor(() => expect(screen.getByRole('button', { name: '刷新' }).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  expect(view().dataset.refreshForce).toBe('true');
});

test('仅 persisted 的 pageshow 触发刷新，普通 pageshow 不触发（A6）', () => {
  render(<App />);
  const key = () => screen.getByTestId('dashboard-refresh-key').textContent;

  firePageShow(false);
  expect(key()).toBe('0');

  firePageShow(true);
  expect(key()).toBe('1');
});

test('布局编辑态期间恢复事件不打断，退出编辑态后恢复正常刷新（A6）', () => {
  render(<App />);
  const key = () => screen.getByTestId('dashboard-refresh-key').textContent;

  act(() => setDashboardEditController(controllerOf(true)));
  setVisibility('visible');
  firePageShow(true);
  expect(key()).toBe('0');

  act(() => setDashboardEditController(controllerOf(false)));
  setVisibility('visible');
  expect(key()).toBe('1');
});

test('卸载时移除 visibilitychange 与 pageshow 监听（A6）', () => {
  const docSpy = vi.spyOn(document, 'removeEventListener');
  const winSpy = vi.spyOn(window, 'removeEventListener');
  const { unmount } = render(<App />);
  unmount();
  expect(docSpy).toHaveBeenCalledWith('visibilitychange', expect.any(Function));
  expect(winSpy).toHaveBeenCalledWith('pageshow', expect.any(Function));
  docSpy.mockRestore();
  winSpy.mockRestore();
});

describe('F05 恢复信号配对', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  const key = () => screen.getByTestId('dashboard-refresh-key').textContent;
  const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });
  const pageHide = () => act(() => { window.dispatchEvent(new Event('pagehide')); });

  function resume(order: 'visible-first' | 'pageshow-first') {
    setVisibility('hidden');
    if (order === 'visible-first') {
      setVisibility('visible');
      advance(25);
      firePageShow(true);
    } else {
      firePageShow(true);
      advance(25);
      setVisibility('visible');
    }
  }

  test.each(['visible-first', 'pageshow-first'] as const)('%s：同次恢复相隔 25ms 的异类信号只刷新一次', (order) => {
    render(<App />);
    resume(order);
    expect(key()).toBe('1');
  });

  test.each(['visible-first', 'pageshow-first'] as const)('%s：100ms 内的第二个 hidden→visible 周期仍独立刷新', (order) => {
    render(<App />);
    resume(order);
    expect(key()).toBe('1');
    advance(25);
    resume(order);
    expect(key()).toBe('2');
  });

  test('首轮仅 visible 时，新一轮 hidden 会切断尚未配对的旧信号', () => {
    render(<App />);
    setVisibility('hidden');
    setVisibility('visible');
    expect(key()).toBe('1');
    advance(25);
    resume('pageshow-first');
    expect(key()).toBe('2');
  });

  test('pagehide 切断配对，紧接的 persisted pageshow 是独立恢复', () => {
    render(<App />);
    setVisibility('visible');
    expect(key()).toBe('1');
    pageHide();
    firePageShow(true);
    expect(key()).toBe('2');
  });

  test('同类信号始终分别处理，异类信号超过 1 秒也不合并', () => {
    render(<App />);
    firePageShow(true);
    advance(25);
    firePageShow(true);
    expect(key()).toBe('2');
    advance(1_000);
    setVisibility('visible');
    expect(key()).toBe('3');
  });

  test('普通 pageshow 不刷新，也不消耗 persisted 信号的配对', () => {
    render(<App />);
    firePageShow(false);
    expect(key()).toBe('0');
    setVisibility('visible');
    firePageShow(false);
    firePageShow(true);
    expect(key()).toBe('1');
  });

  test('两恢复信号之间的手动刷新不被吞掉，也不导致重复恢复刷新', () => {
    render(<App />);
    setVisibility('hidden');
    setVisibility('visible');
    expect(key()).toBe('1');
    // 原按钮的 400ms loading 已结束，但仍在异类恢复信号的 1 秒配对范围内。
    advance(500);
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));
    expect(key()).toBe('2');
    firePageShow(true);
    expect(key()).toBe('2');
  });

  test('布局编辑态跳过整组恢复，退出后新周期正常刷新', () => {
    render(<App />);
    act(() => setDashboardEditController(controllerOf(true)));
    resume('pageshow-first');
    advance(1_000);
    expect(key()).toBe('0');
    act(() => setDashboardEditController(controllerOf(false)));
    resume('visible-first');
    expect(key()).toBe('1');
  });

  test('卸载清理恢复边界监听与尚未完成的刷新定时器', () => {
    const docSpy = vi.spyOn(document, 'removeEventListener');
    const winSpy = vi.spyOn(window, 'removeEventListener');
    const timeoutSpy = vi.spyOn(globalThis, 'setTimeout');
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout');
    const { unmount } = render(<App />);
    setVisibility('visible');
    expect(key()).toBe('1');
    const refreshTimers = () => timeoutSpy.mock.calls.filter(([, delay]) => delay === 400);
    expect(refreshTimers()).toHaveLength(1);
    const timerIndex = timeoutSpy.mock.calls.findIndex(([, delay]) => delay === 400);
    const refreshTimer = timeoutSpy.mock.results[timerIndex].value;
    unmount();
    expect(clearSpy).toHaveBeenCalledWith(refreshTimer);
    expect(docSpy).toHaveBeenCalledWith('visibilitychange', expect.any(Function));
    expect(winSpy).toHaveBeenCalledWith('pageshow', expect.any(Function));
    expect(winSpy).toHaveBeenCalledWith('pagehide', expect.any(Function));
    setVisibility('visible');
    firePageShow(true);
    expect(refreshTimers()).toHaveLength(1);
  });
});
