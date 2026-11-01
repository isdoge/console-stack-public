import { render, screen } from '@testing-library/react';
import { describe, expect, test } from 'vitest';
import {
  BAND_SIZE,
  ProbeBand,
  ProbeBandRow,
  failureRate,
  failureRateTone,
  latencyTone,
  latestLatencyText,
  probeNodeLabel,
} from './probe-band';
import type { ProbeSample } from '@/lib/types';

const up = (ts: string, latency: number | null): ProbeSample => ({
  ts,
  status: 'up',
  latency_ms: latency,
});

const down = (ts: string): ProbeSample => ({ ts, status: 'down', latency_ms: null });

describe('latencyTone（≤100/200/500/1000/>1000 ms）', () => {
  test.each([
    [10, 'bg-signal-1'],
    [100, 'bg-signal-1'],
    [101, 'bg-signal-2'],
    [200, 'bg-signal-2'],
    [201, 'bg-signal-3'],
    [500, 'bg-signal-3'],
    [501, 'bg-signal-4'],
    [1000, 'bg-signal-4'],
    [1001, 'bg-signal-5'],
  ])('%dms → %s', (latency, tone) => {
    expect(latencyTone(latency)).toBe(tone);
  });

  test('缺耗时但可达时按最优档（不因缺数据变红）', () => {
    expect(latencyTone(null)).toBe('bg-signal-1');
  });
});

describe('probeNodeLabel（探测时间节点，固定完整格式）', () => {
  const pad = (n: number) => String(n).padStart(2, '0');

  test('固定 YYYY-MM-DD HH:MM:SS 本地格式，不带 T', () => {
    const d = new Date();
    expect(probeNodeLabel(d.toISOString())).toBe(
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
        `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`,
    );
  });

  test('UTC ISO 串按本地时区换算成同格式', () => {
    const d = new Date('2020-01-02T16:00:00Z');
    expect(probeNodeLabel('2020-01-02T16:00:00Z')).toBe(
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
        `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`,
    );
  });

  test('无法解析的 ts 原样返回，空值给 —', () => {
    expect(probeNodeLabel('')).toBe('—');
    expect(probeNodeLabel('not-a-date')).toBe('not-a-date');
  });
});

describe('ProbeBand（E4）', () => {
  test('无记录时 20 格全空，且空位在玻璃卡底上可见（不用 bg-muted）', () => {
    const { container } = render(<ProbeBand />);
    const cells = container.querySelectorAll('span');
    expect(cells.length).toBe(BAND_SIZE);
    cells.forEach((c) => expect(c.className).toContain('bg-muted-foreground/20'));
  });

  test('不足 20 条时左侧补空，最新一次在最右', () => {
    const { container } = render(
      <ProbeBand samples={[up('t1', 10), { ts: 't2', status: 'down', latency_ms: null }]} />
    );
    const cells = Array.from(container.querySelectorAll('span'));
    expect(cells.length).toBe(BAND_SIZE);
    expect(cells[BAND_SIZE - 1].className).toContain('bg-signal-5');
    expect(cells[BAND_SIZE - 2].className).toContain('bg-signal-1');
    expect(cells[0].className).toContain('bg-muted-foreground/20');
  });

  test('超过 20 条只留最近 20 条', () => {
    const samples = Array.from({ length: 25 }, (_, i) => up(`t${i}`, 500));
    const { container } = render(<ProbeBand samples={samples} />);
    expect(container.querySelectorAll('span').length).toBe(BAND_SIZE);
  });

  test('down 一律红，不看耗时', () => {
    const { container } = render(
      <ProbeBand samples={[{ ts: 't', status: 'down', latency_ms: 5 }]} />
    );
    const cells = Array.from(container.querySelectorAll('span'));
    expect(cells[BAND_SIZE - 1].className).toContain('bg-signal-5');
  });

  test('每格 tooltip 带时间节点与耗时', () => {
    const ts = '2026-08-29T05:00:00Z';
    const { container } = render(<ProbeBand samples={[up(ts, 42)]} />);
    const last = container.querySelectorAll('span')[BAND_SIZE - 1];
    expect(last.getAttribute('title')).toBe(`${probeNodeLabel(ts)} · 42 ms`);
  });

  test('failure 模式 tooltip 同样用时间节点', () => {
    const ts = new Date().toISOString();
    const { container } = render(
      <ProbeBand samples={[down(ts)]} mode="failure" />
    );
    const last = container.querySelectorAll('span')[BAND_SIZE - 1];
    expect(last.getAttribute('title')).toBe(`${probeNodeLabel(ts)} · 失败`);
  });

  test('空态有可读的无障碍标签', () => {
    render(<ProbeBand />);
    expect(screen.getByLabelText('暂无探测记录')).toBeTruthy();
  });

  test('failure 模式只分成败两色，不按耗时分档', () => {
    const { container } = render(
      <ProbeBand samples={[up('t1', 5), up('t2', 900), down('t3')]} mode="failure" />
    );
    const cells = Array.from(container.querySelectorAll('span'));
    // 900ms 在延迟带里是 signal-5，在成败带里仍算成功
    expect(cells[BAND_SIZE - 2].className).toBe(cells[BAND_SIZE - 3].className);
    expect(cells[BAND_SIZE - 1].className).toContain('bg-signal-5');
  });

  test('failure 模式的空态标签说的是成败，不是延迟', () => {
    render(<ProbeBand mode="failure" />);
    expect(screen.getByLabelText('暂无探测成败记录')).toBeTruthy();
  });
});

describe('failureRate（§4.3：探测失败率，刻意不叫丢包）', () => {
  test('无样本 → null，交给调用方决定整行是否渲染', () => {
    expect(failureRate()).toBeNull();
    expect(failureRate([])).toBeNull();
  });

  test('全成功 → 0', () => {
    expect(failureRate([up('t1', 10), up('t2', 12)])).toBe(0);
  });

  test('20 次里错 1 次 → 5%', () => {
    const samples = [...Array.from({ length: 19 }, (_, i) => up(`t${i}`, 10)), down('t19')];
    expect(failureRate(samples)).toBeCloseTo(5, 5);
  });

  test('分母是实际样本数，不是恒定的 20（不足 20 条不稀释失败率）', () => {
    expect(failureRate([up('t1', 10), down('t2')])).toBe(50);
  });

  test('只算最近 20 条，更早的失败不再计入', () => {
    const samples = [
      ...Array.from({ length: 5 }, (_, i) => down(`old${i}`)),
      ...Array.from({ length: 20 }, (_, i) => up(`t${i}`, 10)),
    ];
    expect(failureRate(samples)).toBe(0);
  });

  test('unknown 也算失败（非 up 即失败）', () => {
    expect(failureRate([{ ts: 't', status: 'unknown', latency_ms: null }])).toBe(100);
  });
});

describe('latestLatencyText', () => {
  test('取最新一次的耗时', () => {
    expect(latestLatencyText([up('t1', 10), up('t2', 212)])).toBe('212 ms');
  });

  test('最新一次失败时显示「失败」，不沿用上一次的耗时', () => {
    expect(latestLatencyText([up('t1', 10), down('t2')])).toBe('失败');
  });

  test('可达但没记到耗时 → 「可达」', () => {
    expect(latestLatencyText([up('t1', null)])).toBe('可达');
  });

  test('无样本 → —', () => {
    expect(latestLatencyText()).toBe('—');
  });
});

describe('failureRateTone', () => {
  test('0 不报警（用中性色，别把正常状态染成绿色噪音）', () => {
    expect(failureRateTone(0)).toBe('text-muted-foreground');
    expect(failureRateTone(null)).toBe('text-muted-foreground');
  });

  test('≤5% 黄，更高红', () => {
    expect(failureRateTone(5)).toBe('text-signal-4');
    expect(failureRateTone(5.1)).toBe('text-signal-5');
    expect(failureRateTone(50)).toBe('text-signal-5');
  });
});

describe('ProbeBandRow（§4.1 第四区：左延迟 / 右失败率）', () => {
  test('两块等宽（grid-cols-2），各带一条 20 格色带', () => {
    const { container } = render(<ProbeBandRow samples={[up('t1', 212)]} />);
    expect(container.firstElementChild!.className).toContain('grid-cols-2');
    expect(container.querySelectorAll('[role="img"]').length).toBe(2);
    expect(container.querySelectorAll('span').length).toBe(BAND_SIZE * 2 + 4);
  });

  test('两侧 header 各带标签与当前值', () => {
    render(<ProbeBandRow samples={[up('t1', 10), down('t2')]} />);
    expect(screen.getByText('延迟')).toBeTruthy();
    expect(screen.getByText('失败')).toBeTruthy();
    expect(screen.getByText('探测失败率')).toBeTruthy();
    expect(screen.getByText('50.0%')).toBeTruthy();
  });

  test('失败率标题写清不是丢包率（口径写在 UI 上，不只在文档里）', () => {
    const { container } = render(<ProbeBandRow samples={[up('t1', 10)]} />);
    const titled = container.querySelector('[title*="不是丢包率"]');
    expect(titled).not.toBeNull();
  });
});
