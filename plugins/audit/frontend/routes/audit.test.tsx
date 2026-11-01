import { MemoryRouter } from 'react-router-dom';
import type { ReactElement } from 'react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { act, fireEvent, render as rawRender, screen, waitFor } from '@testing-library/react';
import { toast } from 'sonner';
import { AuditRoute } from './audit';
import { listMonitorEvents, listOplogs } from '@/lib/api';
import type { MonitorEventPage, OpLog } from '@/lib/types';

vi.mock('@/lib/api', () => ({
  listMonitorEvents: vi.fn(),
  listOplogs: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const mockedEvents = vi.mocked(listMonitorEvents);
const mockedOplogs = vi.mocked(listOplogs);

const emptyPage: MonitorEventPage = {
  events: [],
  total: 0,
  limit: 50,
  offset: 0,
  active_count: 0,
  window_days: 7,
  generated_at: '2026-09-07T12:00:00Z',
};

const oplog: OpLog = {
  id: 1,
  host: 'nas',
  svc: 'axonhub',
  action: 'restart',
  started_at: '2026-09-07 10:00:00',
  result: 'Success',
  operator: 'self',
};

beforeEach(() => {
  vi.clearAllMocks();
  mockedEvents.mockReset();
  mockedOplogs.mockReset();
  mockedEvents.mockResolvedValue(emptyPage);
  mockedOplogs.mockResolvedValue([oplog]);
});

test('读取失败可见且可重试，刷新失败保留已有记录', async () => {
  mockedOplogs.mockRejectedValueOnce(new Error('offline'));
  const { rerender } = render(<AuditRoute refreshKey={0} />);
  fireEvent.mouseDown(screen.getByRole('tab', { name: '操作记录' }));
  await screen.findByRole('alert');
  expect(screen.queryByText('暂无操作记录')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  await screen.findByText('axonhub');
  mockedOplogs.mockRejectedValueOnce(new Error('offline'));
  rerender(<AuditRoute refreshKey={1} />);
  await screen.findByRole('alert');
  expect(screen.getByText('axonhub')).toBeTruthy();
});

test('离开操作记录后的迟到失败不产生 toast', async () => {
  let reject!: (error: Error) => void;
  mockedOplogs.mockReturnValueOnce(new Promise((_, rej) => { reject = rej; }));
  render(<AuditRoute refreshKey={0} />);
  fireEvent.mouseDown(screen.getByRole('tab', { name: '操作记录' }));
  await waitFor(() => expect(mockedOplogs).toHaveBeenCalled());
  fireEvent.mouseDown(screen.getByRole('tab', { name: '监控事件' }));
  await act(async () => reject(new Error('late failure')));
  expect(toast.error).not.toHaveBeenCalled();
});

describe('AuditRoute 双 tab', () => {
  test('默认落在「监控事件」，只拉事件不拉 oplog', async () => {
    render(<AuditRoute refreshKey={0} />);
    await waitFor(() => expect(mockedEvents).toHaveBeenCalled());
    expect(screen.getByRole('tab', { name: '监控事件' })).toBeTruthy();
    expect(mockedOplogs).not.toHaveBeenCalled();
  });

  test('切到「操作记录」才拉 oplog，并渲染表格', async () => {
    render(<AuditRoute refreshKey={0} />);
    await waitFor(() => expect(mockedEvents).toHaveBeenCalled());
    // Radix Tabs 的 Trigger 走 mousedown，不是 click
    fireEvent.mouseDown(screen.getByRole('tab', { name: '操作记录' }));
    await waitFor(() => expect(mockedOplogs).toHaveBeenCalled());
    expect(await screen.findByText('axonhub')).toBeTruthy();
    expect(screen.getByText('restart')).toBeTruthy();
  });

  test('事件与 oplog 是两条独立数据流（切 tab 不会重复请求对方）', async () => {
    render(<AuditRoute refreshKey={0} />);
    await waitFor(() => expect(mockedEvents).toHaveBeenCalledTimes(1));
    // Radix Tabs 的 Trigger 走 mousedown，不是 click
    fireEvent.mouseDown(screen.getByRole('tab', { name: '操作记录' }));
    await waitFor(() => expect(mockedOplogs).toHaveBeenCalledTimes(1));
    expect(mockedEvents).toHaveBeenCalledTimes(1);
  });
});

function render(ui: ReactElement) {
  return rawRender(ui, { wrapper: MemoryRouter });
}


test('服务深链直达操作记录，同名跨机器和相似服务不会混入', async () => {
  mockedOplogs.mockResolvedValue([
    { ...oplog, host: 'txy' }, { ...oplog, id: 2, host: 'us' },
    { ...oplog, id: 3, host: 'txy', svc: 'axonhub-proxy' },
  ]);
  rawRender(<MemoryRouter initialEntries={['/audit?tab=oplog&q=axonhub&host=txy&svc=axonhub']}><AuditRoute refreshKey={0} /></MemoryRouter>);
  await screen.findByText('axonhub');
  expect(screen.queryByText('us')).toBeNull();
  expect(screen.queryByText('axonhub-proxy')).toBeNull();
  expect(mockedOplogs).toHaveBeenCalledWith('axonhub');
});
