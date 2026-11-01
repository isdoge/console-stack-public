import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import {
  DeviceHistoryChart,
  GAP_REASON_LABELS,
  RESOLUTION_LABELS,
  formatSeriesValue,
  pickResolution,
  splitSeriesSegments,
  thinPoints,
} from './device-history-chart';
import type { MonitorEvent } from '@/lib/types';

const T0 = Date.UTC(2026, 9, 24, 0, 0, 0); // 2026-10-24T00:00:00Z
const MIN = 60_000;

function pt(offsetMin: number, value: number) {
  return { ts: new Date(T0 + offsetMin * MIN).toISOString(), value };
}

function gap(startMin: number, endMin: number, reason: 'no_samples' | 'retention_exceeded' | 'archived' | 'unsupported' = 'no_samples') {
  return {
    start: new Date(T0 + startMin * MIN).toISOString(),
    end: new Date(T0 + endMin * MIN).toISOString(),
    reason,
  };
}

const WINDOW = {
  start: new Date(T0).toISOString(),
  end: new Date(T0 + 6 * 60 * MIN).toISOString(),
};

// ---------------------------------------------------------------------------
// B1-3 §6.4 渲染规则：缺口断线、不补 0、不插值、计数器 reset 断线
// ---------------------------------------------------------------------------

describe('splitSeriesSegments（B1-3 §6.4 缺口断线）', () => {
  test('显式 gap 区间跨过的相邻点不连线', () => {
    const r = splitSeriesSegments({
      metric: 'disk_used',
      points: [pt(0, 40), pt(5, 41), pt(30, 42), pt(35, 43)],
      gaps: [gap(10, 25)],
    });
    expect(r.segments.length).toBe(2);
  });

  test('超过 2.5×interval 的断更即使无显式 gap 也断线（12 分钟阈值语义）', () => {
    const r = splitSeriesSegments({
      metric: 'cpu',
      interval_sec: 300,
      points: [pt(0, 10), pt(5, 11), pt(40, 12)], // 5→40 分钟 = 35min > 12.5min
    });
    expect(r.segments.length).toBe(2);
  });

  test('尾缘不足新鲜度阈值的缺席不判缺口：线停在最后样本处', () => {
    const r = splitSeriesSegments({
      metric: 'cpu',
      interval_sec: 300,
      points: [pt(0, 10), pt(5, 11), pt(10, 12)], // 连续 5 分钟采样
    });
    expect(r.segments.length).toBe(1);
    expect(r.gaps.length).toBe(0);
  });

  test('计数器变小（reset 键齿）断线且不算缺口：net_sent_total 回退', () => {
    const r = splitSeriesSegments({
      metric: 'net_sent_total',
      points: [pt(0, 1e12), pt(5, 1.1e12), pt(10, 2e8), pt(15, 3e8)], // 10min 处归零
    });
    expect(r.segments.length).toBe(2);
    expect(r.resets).toBe(1);
    expect(r.gaps.length).toBe(0);
  });

  test('利用率指标回退不算 reset：cpu 下降是正常波动', () => {
    const r = splitSeriesSegments({
      metric: 'cpu',
      points: [pt(0, 80), pt(5, 20)],
    });
    expect(r.segments.length).toBe(1);
    expect(r.resets).toBe(0);
  });

  test('null/非法点直接丢弃，不补 0', () => {
    const r = splitSeriesSegments({
      metric: 'cpu',
      // B1-1 §4.3 {t,v} 形态 + 非法值混入
      points: [
        { t: new Date(T0).toISOString(), v: 10 },
        { t: new Date(T0 + 5 * MIN).toISOString(), v: Number.NaN },
        { t: new Date(T0 + 10 * MIN).toISOString(), v: 12 },
      ] as never,
    });
    expect(r.segments.flat().length).toBe(2);
    expect(r.segments[0][0].value).toBe(10);
  });
});

describe('thinPoints（§6.4 抽稀不得造中间值）', () => {
  test('抽稀只输出原始点对象，不造中间值', () => {
    const points = Array.from({ length: 100 }, (_, i) => ({ ts: T0 + i * MIN, value: i }));
    const out = thinPoints(points, 20);
    expect(out.length).toBeLessThanOrEqual(20);
    // 每个输出点都是输入的原始对象（引用相等 = 没有插值）
    const set = new Set(points);
    for (const p of out) expect(set.has(p)).toBe(true);
  });

  test('桶内极值被保留（保形），且时间序不乱', () => {
    const points = Array.from({ length: 60 }, (_, i) => ({ ts: T0 + i * MIN, value: i % 7 }));
    const out = thinPoints(points, 10);
    for (let i = 1; i < out.length; i += 1) expect(out[i].ts).toBeGreaterThanOrEqual(out[i - 1].ts);
    // 全距极值（0 与 6）都应出现在输出里
    expect(out.some((p) => p.value === 0)).toBe(true);
    expect(out.some((p) => p.value === 6)).toBe(true);
  });

  test('点数不超限时不抽稀', () => {
    const points = [{ ts: T0, value: 1 }, { ts: T0 + MIN, value: 2 }];
    expect(thinPoints(points, 10)).toBe(points);
  });
});

describe('pickResolution（B1-3 §2.2 自动档位）', () => {
  test('≤24h → raw，>24h → hourly', () => {
    expect(pickResolution(24 * 3600 * 1000)).toBe('raw');
    expect(pickResolution(24 * 3600 * 1000 + 1)).toBe('hourly');
    expect(pickResolution(7 * 24 * 3600 * 1000)).toBe('hourly');
  });
});

describe('formatSeriesValue', () => {
  test('percent / bytes/s / GB / °C / 计数', () => {
    expect(formatSeriesValue(43.25, 'percent')).toBe('43.3%');
    expect(formatSeriesValue(1536 * 1024, 'bytes/s')).toContain('M/s');
    expect(formatSeriesValue(40.24, 'GB')).toBe('40.2 GB');
    expect(formatSeriesValue(61.4, '°C')).toBe('61 °C');
    expect(formatSeriesValue(0.42, 'count')).toBe('0.42');
  });
});

// ---------------------------------------------------------------------------
// 组件渲染：统一时间窗 / 曲线联动 / 事件标记 / 版本与健康分离
// ---------------------------------------------------------------------------

const EVENT: MonitorEvent = {
  id: 7,
  fingerprint: 'machine:nas',
  kind: 'machine',
  source: 'probe',
  target: 'nas',
  machine: 'nas',
  severity: 'error',
  title: '机器离线',
  detail: '',
  opened_at: new Date(T0 + 60 * MIN).toISOString(),
  updated_at: new Date(T0 + 60 * MIN).toISOString(),
  resolved_at: new Date(T0 + 90 * MIN).toISOString(),
  state: 'resolved',
  duration_sec: 1800,
};

function baseHistory() {
  // 采样间隔与 interval_sec:300 一致（每 5 分钟一点），gap 区间内无点
  return {
    resolution: 'raw' as const,
    window: WINDOW,
    series: [
      {
        metric: 'cpu',
        unit: 'percent',
        interval_sec: 300,
        // 0–30min 连续；35–55 gap；60–120 连续
        points: [
          pt(0, 10), pt(5, 11), pt(10, 12), pt(15, 13), pt(20, 14), pt(25, 15), pt(30, 16),
          pt(60, 30), pt(65, 31), pt(70, 32), pt(75, 33), pt(80, 34), pt(85, 35), pt(90, 36),
          pt(95, 37), pt(100, 38), pt(105, 39), pt(110, 40), pt(115, 41), pt(120, 42),
        ],
        gaps: [gap(35, 55)],
      },
      {
        metric: 'disk_used',
        unit: 'GB',
        interval_sec: 300,
        // 10–50 gap
        points: [pt(0, 100), pt(5, 101), pt(55, 110), pt(60, 111), pt(120, 120)],
        gaps: [gap(10, 50)],
      },
    ],
  };
}

describe('DeviceHistoryChart 渲染', () => {
  test('多序列共用统一时间轴：两条泳道 + 事件泳道 + 轴刻度', () => {
    render(
      <DeviceHistoryChart
        machine={{ id: 'nas', label: 'NAS', role: 'server', probe_status: 'up' }}
        history={baseHistory()}
        events={[EVENT]}
      />
    );
    expect(screen.getByLabelText(/历史曲线图/)).toBeTruthy();
    expect(document.querySelectorAll('[data-lane]').length).toBe(2);
    expect(document.querySelectorAll('[data-event-bar="7"]').length).toBe(1);
    expect(document.querySelector('[data-time-axis]')).toBeTruthy();
    // 分辨率档位徽章（B1-3 §2.2 回显）
    expect(document.querySelector('[data-resolution="raw"]')?.textContent).toContain('原始采样');
  });

  test('缺口显式渲染且标原因，不连线', () => {
    render(<DeviceHistoryChart history={baseHistory()} />);
    const cpuGaps = document.querySelectorAll('[data-gap-reason="no_samples"]');
    expect(cpuGaps.length).toBeGreaterThanOrEqual(2);
    // 每条曲线断成两段（跨 gap 不连线）
    const polylines = document.querySelectorAll('polyline');
    expect(polylines.length).toBe(4); // 2 序列 × 2 段
  });

  test('hover 联动：一条泳道悬停出现游标与 tooltip，读数取最近真实点', () => {
    render(<DeviceHistoryChart history={baseHistory()} />);
    const plot = screen.getByLabelText(/历史曲线图/);
    const rect = { left: 0, width: 100, top: 0, height: 100 };
    vi.spyOn(document.querySelector('[data-history-time-domain]')!, 'getBoundingClientRect').mockReturnValue(rect as DOMRect);
    fireEvent.mouseMove(plot, { clientX: 25 });
    expect(document.querySelector('[data-history-cursor]')).toBeTruthy();
    const tooltip = document.querySelector('[data-history-tooltip]');
    expect(tooltip).toBeTruthy();
    // 25% ≈ 90 分钟处：cpu 有真实采样点（36%），tooltip 列出读数
    expect(tooltip?.textContent).toContain('CPU');
  });

  test('hover 落在缺口上：不显示最近点冒充（无样本即明示）', () => {
    render(<DeviceHistoryChart history={baseHistory()} />);
    const plot = screen.getByLabelText(/历史曲线图/);
    vi.spyOn(document.querySelector('[data-history-time-domain]')!, 'getBoundingClientRect').mockReturnValue({ left: 0, width: 100 } as DOMRect);
    // cpu 的 gap 是 35–55 分钟，窗口 0–360 分钟 → 9.7%–15.3%，取中点 ~12.5%
    fireEvent.mouseMove(plot, { clientX: 12.5 });
    const tooltip = document.querySelector('[data-history-tooltip]');
    expect(tooltip?.textContent).toContain('该时刻无样本');
  });

  test('事件区间条按 severity 着色，title 含标题与持续', () => {
    render(<DeviceHistoryChart history={baseHistory()} events={[EVENT]} />);
    const bar = document.querySelector('[data-event-bar="7"]') as HTMLElement;
    expect(bar.title).toContain('机器离线');
    expect(bar.title).toContain('已恢复');
    expect(bar.title).toContain('30 分钟');
  });

  test('单点可见，缺采空窗仍显示同窗事件', () => {
    const { rerender } = render(<DeviceHistoryChart history={{ ...baseHistory(), series: [{ metric: 'cpu', points: [pt(0, 1)] }] }} />);
    expect(document.querySelector('[data-history-single-point]')).toBeTruthy();
    rerender(<DeviceHistoryChart history={{ ...baseHistory(), series: [{ metric: 'cpu', points: [] }] }} events={[EVENT]} />);
    expect(document.querySelector('[data-event-bar="7"]')).toBeTruthy();
    expect(screen.getByText('暂无样本')).toBeTruthy();
  });

  test('窗口外事件不渲染刻度', () => {
    const outside: MonitorEvent = {
      ...EVENT,
      id: 8,
      opened_at: new Date(T0 - 24 * 60 * MIN).toISOString(),
      resolved_at: new Date(T0 - 23 * 60 * MIN).toISOString(),
    };
    render(<DeviceHistoryChart history={baseHistory()} events={[outside]} />);
    expect(document.querySelector('[data-event-bar="8"]')).toBeNull();
  });

  test('loading 骨架与空态显式表达（不画假曲线）', () => {
    const { rerender } = render(<DeviceHistoryChart loading history={baseHistory()} />);
    expect(document.querySelector('[data-history-loading]')).toBeTruthy();

    rerender(<DeviceHistoryChart history={null} />);
    expect(document.querySelector('[data-history-state="empty"]')).toBeTruthy();

    rerender(
      <DeviceHistoryChart
        history={{
          series: [{ metric: 'temp', points: [], gaps: [{ ...gap(0, 360), reason: 'unsupported' }] }],
        }}
      />
    );
    expect(document.querySelector('[data-history-state="unsupported"]')).toBeTruthy();
  });

  test('B1-1 §5.4：更新提示用 sky 信息色徽章，与健康状态视觉分离', () => {
    render(
      <DeviceHistoryChart
        machine={{ id: 'nas', label: 'NAS', role: 'server', probe_status: 'up' }}
        snapshot={{
          machine_id: 'nas',
          agent_version: '0.19.0',
          agent_update_available: true,
          data_quality: 'fresh',
        }}
        history={baseHistory()}
      />
    );
    const updateBadge = document.querySelector('[data-agent-update-badge]');
    expect(updateBadge?.textContent).toBe('可更新');
    // 更新徽章是 sky 信息色；质量徽章 fresh 不渲染（无故障不标异常）
    expect(updateBadge?.className).toContain('sky');
    expect(document.querySelector('[data-quality-badge]')).toBeNull();
  });

  test('健康异常走 signal 语义色徽章，与更新提示互不掺色', () => {
    render(
      <DeviceHistoryChart
        snapshot={{ machine_id: 'nas', data_quality: 'stale' }}
        history={baseHistory()}
      />
    );
    const badge = document.querySelector('[data-quality-badge="stale"]');
    expect(badge?.textContent).toContain('数据过期');
    expect(badge?.className).not.toContain('sky');
  });

  test('无 window 时以点/事件时间并集建轴（仍可渲染）', () => {
    render(
      <DeviceHistoryChart
        history={{ series: [{ metric: 'cpu', points: [pt(0, 5), pt(120, 9)] }] }}
        events={[EVENT]}
      />
    );
    expect(document.querySelectorAll('[data-lane]').length).toBe(1);
  });
});

describe('常量', () => {
  test('缺口原因与档位中文标签与契约一致（B1-3 §3.1/§2.2）', () => {
    expect(GAP_REASON_LABELS).toEqual({
      no_samples: '未采到',
      retention_exceeded: '已过期',
      archived: '已归档',
      unsupported: '不支持',
    });
    expect(RESOLUTION_LABELS.raw).toBe('原始采样');
    expect(RESOLUTION_LABELS.hourly).toBe('小时聚合');
  });
});
