import { displayTime } from '@/lib/display-time';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { EventTimeline, formatEventDuration, formatEventTime } from './event-timeline';
import { listMonitorEvents } from '@/lib/api';
import type { MonitorEvent, MonitorEventPage } from '@/lib/types';

vi.mock('@/lib/api', () => ({
  listMonitorEvents: vi.fn(),
}));

const mockedList = vi.mocked(listMonitorEvents);

const event = (overrides: Partial<MonitorEvent> = {}): MonitorEvent => ({
  id: 1,
  fingerprint: 'machine:nas',
  kind: 'machine',
  source: 'probe',
  target: 'nas',
  machine: 'nas',
  severity: 'error',
  title: 'NAS 探测失败',
  detail: 'tcp refused',
  opened_at: '2026-09-07T10:00:00Z',
  updated_at: '2026-09-07T10:00:00Z',
  resolved_at: null,
  state: 'active',
  duration_sec: 3600,
  ...overrides,
});

const pageOf = (events: MonitorEvent[], overrides: Partial<MonitorEventPage> = {}): MonitorEventPage => ({
  events,
  total: events.length,
  limit: 50,
  offset: 0,
  active_count: events.filter((e) => e.state === 'active').length,
  window_days: 7,
  generated_at: '2026-09-07T12:00:00Z',
  ...overrides,
});

/** 只取徽章，排除同文案的筛选按钮。 */
const badgesWithText = (text: string) =>
  screen.getAllByText(text).filter((el) => el.tagName !== 'BUTTON');

beforeEach(() => {
  mockedList.mockReset();
});

describe('formatEventTime', () => {
  test('ISO 转可读；空值给破折号', () => {
    expect(formatEventTime('2026-09-07T10:00:00Z')).toBe(displayTime('2026-09-07T10:00:00Z').text);
    expect(formatEventTime(null)).toBe('—');
    expect(formatEventTime(undefined)).toBe('—');
  });
});

describe('formatEventDuration', () => {
  test('秒 / 分 / 时 / 天四档', () => {
    expect(formatEventDuration(30)).toBe('30 秒');
    expect(formatEventDuration(90)).toBe('1 分钟');
    expect(formatEventDuration(3600 + 300)).toBe('1 小时 5 分');
    expect(formatEventDuration(86400 * 2 + 3600 * 3)).toBe('2 天 3 小时');
  });

  test('null / 负数 / NaN 一律破折号，不显示成 0 秒', () => {
    expect(formatEventDuration(null)).toBe('—');
    expect(formatEventDuration(undefined)).toBe('—');
    expect(formatEventDuration(-5)).toBe('—');
    expect(formatEventDuration(Number.NaN)).toBe('—');
  });
});

describe('EventTimeline 渲染', () => {
  test('同属 machine 的规则告警与采集中断按 fingerprint 显式分类', () => {
    render(<EventTimeline page={pageOf([
      event({ fingerprint: 'machine:alert:nas:Disk', title: 'NAS 磁盘规则' }),
      event({ id: 2, fingerprint: 'machine:monitoring:nas', title: 'NAS 指标停更' }),
      event({ id: 3, fingerprint: 'machine:nas', title: 'NAS 普通探测' }),
    ])} />);
    expect(screen.getAllByText('规则告警')).toHaveLength(1);
    expect(screen.getAllByText('采集中断')).toHaveLength(1);
    expect(screen.getByText('NAS 普通探测')).toBeTruthy();
    expect(screen.getAllByTestId('event-row')).toHaveLength(3);
  });
  test('活动事件显示「未恢复」，已恢复显示「已恢复」与恢复时间', () => {
    render(
      <EventTimeline
        page={pageOf([
          event(),
          event({
            id: 2,
            fingerprint: 'service:svc-a',
            kind: 'service',
            target: 'svc-a',
            title: 'svc-a 健康检查失败',
            resolved_at: '2026-09-07T11:00:00Z',
            state: 'resolved',
            duration_sec: 7200,
          }),
        ])}
      />
    );
    // 「未恢复」「已恢复」同时是状态筛选按钮的文案（词汇表刻意一致），只数徽章
    expect(badgesWithText('未恢复')).toHaveLength(1);
    expect(badgesWithText('已恢复')).toHaveLength(1);
    expect(screen.getByText(displayTime('2026-09-07T11:00:00Z').text)).toBeTruthy();
  });

  test('根因事件显示「影响 N 项」，影响面事件缩进', () => {
    render(
      <EventTimeline
        page={pageOf([
          event({ impact_count: 2 }),
          event({
            id: 2,
            fingerprint: 'service:svc-a',
            kind: 'service',
            target: 'svc-a',
            title: 'svc-a 健康检查失败',
            collapsed_by: 'machine:nas',
          }),
        ])}
      />
    );
    expect(screen.getByText('影响 2 项')).toBeTruthy();
    const rows = screen.getAllByTestId('event-row');
    expect(rows[0].className).not.toContain('pl-10');
    expect(rows[1].className).toContain('pl-10');
  });

  test('impact_count 为 0 时不显示影响徽章（不写「影响 0 项」）', () => {
    render(<EventTimeline page={pageOf([event({ impact_count: 0 })])} />);
    expect(screen.queryByText(/影响/)).toBeNull();
  });

  test('空列表给正向空态而不是空白', () => {
    render(<EventTimeline page={pageOf([])} />);
    expect(screen.getByText('该窗口内没有状态变化')).toBeTruthy();
  });

  test('摘要显示未恢复条数', () => {
    render(<EventTimeline page={pageOf([event(), event({ id: 2 })])} />);
    expect(screen.getByText('2 项未恢复 · 共 2 条')).toBeTruthy();
  });

  test('全部已恢复时摘要只报总数', () => {
    render(
      <EventTimeline
        page={pageOf([event({ state: 'resolved', resolved_at: '2026-09-07T11:00:00Z' })])}
      />
    );
    expect(screen.getByText('共 1 条')).toBeTruthy();
  });
});

describe('EventTimeline 取数与筛选', () => {
  test('默认按 7 天窗口、state=all 拉取', async () => {
    mockedList.mockResolvedValue(pageOf([]));
    render(<EventTimeline />);
    await waitFor(() => expect(mockedList).toHaveBeenCalled());
    expect(mockedList).toHaveBeenCalledWith({
      days: 7,
      kind: undefined,
      machine: undefined,
      state: 'all',
      limit: 50,
      offset: 0,
    });
  });

  test('切类型筛选会带上 kind 重新拉取', async () => {
    mockedList.mockResolvedValue(pageOf([]));
    render(<EventTimeline />);
    await waitFor(() => expect(mockedList).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '机器' }));
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        expect.objectContaining({ kind: 'machine' })
      )
    );
  });

  test('公网筛选带上 kind=public，公网事件照常成行', async () => {
    mockedList.mockResolvedValue(
      pageOf([
        event({
          fingerprint: 'public:us',
          kind: 'public',
          source: 'public_probe',
          target: 'us',
          machine: 'us',
          severity: 'warn',
          title: 'Oracle 家宽公网不通',
          detail: 'tcp us.sunxc.eu.cc:62037 timed out',
        }),
      ])
    );
    render(<EventTimeline />);
    await waitFor(() => expect(mockedList).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '公网' }));
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ kind: 'public' }))
    );
    expect(await screen.findByText('Oracle 家宽公网不通')).toBeTruthy();
    expect(screen.getByText('tcp us.sunxc.eu.cc:62037 timed out')).toBeTruthy();
  });

  test('切窗口会带上 days 重新拉取', async () => {
    mockedList.mockResolvedValue(pageOf([]));
    render(<EventTimeline />);
    await waitFor(() => expect(mockedList).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '90 天' }));
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ days: 90 }))
    );
  });

  test('换筛选条件时回到第一页（否则会落在新结果集之外显示成空）', async () => {
    mockedList.mockResolvedValue(pageOf([event()], { total: 200 }));
    render(<EventTimeline />);
    await waitFor(() => expect(mockedList).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 }))
    );
    fireEvent.click(screen.getByRole('button', { name: '服务' }));
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        expect.objectContaining({ kind: 'service', offset: 0 })
      )
    );
    expect(mockedList.mock.calls.filter(([query]) => query?.kind === 'service')).toHaveLength(1);
  });

  test('结果收缩时自动退回有效末页', async () => {
    mockedList.mockResolvedValue(pageOf([event()], { total: 120 }));
    const { rerender } = render(<EventTimeline refreshKey={0} />);
    fireEvent.click(await screen.findByRole('button', { name: '下一页' }));
    await screen.findByText('51–100 / 120');
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByText('101–120 / 120');
    mockedList.mockResolvedValueOnce(pageOf([], { total: 60, offset: 100 }))
      .mockResolvedValueOnce(pageOf([event()], { total: 60, offset: 50 }));
    rerender(<EventTimeline refreshKey={1} />);
    await screen.findByText('51–60 / 60');
    expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 }));
  });

  test('结果归零时回首页，读取失败可直接重试', async () => {
    mockedList.mockResolvedValue(pageOf([event()], { total: 70 }));
    const { rerender } = render(<EventTimeline refreshKey={0} />);
    fireEvent.click(await screen.findByRole('button', { name: '下一页' }));
    await screen.findByText('51–70 / 70');
    mockedList.mockResolvedValue(pageOf([]));
    rerender(<EventTimeline refreshKey={1} />);
    await screen.findByText('该窗口内没有状态变化');
    await waitFor(() => expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0 })));
    expect(screen.queryByRole('button', { name: '上一页' })).toBeNull();
    mockedList.mockRejectedValueOnce(new Error('offline'));
    rerender(<EventTimeline refreshKey={2} />);
    fireEvent.click(await screen.findByRole('button', { name: '重试' }));
    await screen.findByText('该窗口内没有状态变化');
  });

  test('机器筛选项从已见事件累积，且「只增不减」（筛完不会把自己锁死）', async () => {
    mockedList.mockResolvedValueOnce(
      pageOf([event({ machine: 'nas' }), event({ id: 2, machine: 'txy' })])
    );
    render(<EventTimeline />);
    await waitFor(() => expect(mockedList).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('button', { name: 'nas' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'txy' })).toBeTruthy();

    // 筛到 nas 后结果里只剩 nas 的事件，txy 的 chip 必须还在
    mockedList.mockResolvedValueOnce(pageOf([event({ machine: 'nas' })]));
    fireEvent.click(screen.getByRole('button', { name: 'nas' }));
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        expect.objectContaining({ machine: 'nas', offset: 0 })
      )
    );
    expect(screen.getByRole('button', { name: 'txy' })).toBeTruthy();
  });

  test('只有一台机器出过事时不渲染机器筛选行', async () => {
    mockedList.mockResolvedValue(pageOf([event({ machine: 'nas' })]));
    render(<EventTimeline />);
    await waitFor(() => expect(mockedList).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'nas' })).toBeNull();
  });

  test('接口失败退化为错误提示，不白屏', async () => {
    mockedList.mockRejectedValue(new Error('boom'));
    render(<EventTimeline />);
    expect(await screen.findByText('事件列表加载失败')).toBeTruthy();
    expect(screen.getByText('加载失败')).toBeTruthy();
  });

  test('总数在一页内时不渲染翻页按钮', () => {
    render(<EventTimeline page={pageOf([event()])} />);
    expect(screen.queryByRole('button', { name: '下一页' })).toBeNull();
  });

  test('翻页按钮在首页禁用「上一页」', async () => {
    mockedList.mockResolvedValue(pageOf([event()], { total: 200 }));
    render(<EventTimeline />);
    await waitFor(() => expect(mockedList).toHaveBeenCalled());
    expect(
      (screen.getByRole('button', { name: '上一页' }) as HTMLButtonElement).disabled
    ).toBe(true);
    expect(
      (screen.getByRole('button', { name: '下一页' }) as HTMLButtonElement).disabled
    ).toBe(false);
  });
});
