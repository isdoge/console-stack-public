import { displayTime } from '@/lib/display-time';
import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ObservabilitySummary, formatObservabilityTime } from './observability-summary';
import type { ObservabilityPayload } from '@/lib/types';

const BASE: ObservabilityPayload = {
  ok: true,
  updated_at: '2026-08-29T19:20:00Z',
  probe_interval: 600,
  source_errors: [],
  machine_links: { aly: '/beszel/system/abc' },
  active_alerts: [],
  recent_events: [],
  systemd_services: [],
  smart_devices: [],
};

function alertFor(i: number) {
  return {
    machine: 'aly',
    name: `规则${i}`,
    severity: 'error' as const,
    threshold: 0,
    duration_min: 5,
    triggered_at: '2026-08-29T18:37:42Z',
  };
}

describe('ObservabilitySummary（§4.2 / D2）', () => {
  test('null / 空数组都不渲染，不占一张大空卡', () => {
    const { container } = render(<ObservabilitySummary data={null} />);
    expect(container.firstChild).toBeNull();
    const { container: c2 } = render(<ObservabilitySummary data={BASE} />);
    expect(c2.firstChild).toBeNull();
  });

  test('PG 没有事件仍显示能力缺口，不冒充健康空列表', () => {
    render(<ObservabilitySummary data={{
      ...BASE,
      source: 'console_pg',
      capabilities: { systemd: 'unsupported', smart: 'unsupported' },
    }} />);
    expect(screen.getByText('2 项未覆盖')).toBeTruthy();
    expect(screen.getByText(/空列表不代表检查通过/)).toBeTruthy();
  });

  test('source_errors 显示「部分监控数据暂不可用」，其余数组仍展示', () => {
    render(
      <ObservabilitySummary
        data={{
          ...BASE,
          ok: false,
          source_errors: [{ source: 'systemd_services', message: 'HTTP 503' }],
          active_alerts: [alertFor(1)],
        }}
      />
    );
    expect(screen.getByText('部分监控数据暂不可用')).toBeTruthy();
    expect(screen.getByText(/systemd_services: HTTP 503/)).toBeTruthy();
    expect(screen.getByText(/规则1/)).toBeTruthy();
  });

  test('活动告警最多 5 条', () => {
    render(
      <ObservabilitySummary
        data={{ ...BASE, active_alerts: Array.from({ length: 7 }).map((_, i) => alertFor(i)) }}
      />
    );
    expect(screen.getByText(/规则0/)).toBeTruthy();
    expect(screen.getByText(/规则4/)).toBeTruthy();
    expect(screen.queryByText(/规则5/)).toBeNull();
    expect(screen.getAllByText('触发中')).toHaveLength(5);
  });

  test('事件 active 与 resolved 文案可区分', () => {
    render(
      <ObservabilitySummary
        data={{
          ...BASE,
          recent_events: [
            {
              machine: 'aly',
              name: 'Status',
              severity: 'error',
              threshold: 0,
              opened_at: '2026-08-29T18:37:42Z',
              resolved_at: null,
              state: 'active',
              duration_sec: null,
            },
            {
              machine: 'nas',
              name: 'Disk',
              severity: 'warn',
              threshold: 85,
              opened_at: '2026-08-29T10:00:00Z',
              resolved_at: '2026-08-29T10:30:00Z',
              state: 'resolved',
              duration_sec: 1800,
            },
          ],
        }}
      />
    );
    expect(screen.getByText('进行中')).toBeTruthy();
    expect(screen.getByText('已恢复')).toBeTruthy();
    // resolved 事件的时长只对已恢复展示
    expect(screen.getByText(/· 1800s/)).toBeTruthy();
  });

  test('PG 事件不渲染已退役 Hub 链接', () => {
    render(<ObservabilitySummary data={{ ...BASE, active_alerts: [alertFor(1)] }} />);
    expect(screen.queryByRole('link')).toBeNull();
  });

  test('机器 id 用台账 label 展示，取不到时大写', () => {
    render(
      <ObservabilitySummary
        data={{ ...BASE, active_alerts: [alertFor(1)] }}
        machines={[{ id: 'aly', label: '阿里云 ALY', role: 'server' }]}
      />
    );
    expect(screen.getByText('阿里云 ALY')).toBeTruthy();
  });

  test('formatObservabilityTime 确定性输出且空值回 —', () => {
    expect(formatObservabilityTime('2026-08-29T18:37:42Z')).toBe(displayTime('2026-08-29T18:37:42Z').text);
    expect(formatObservabilityTime()).toBe('—');
  });

  test('长告警/事件名截断且有 title（D7）', () => {
    const longName = 'StatusRule'.repeat(20);
    render(
      <ObservabilitySummary
        data={{
          ...BASE,
          active_alerts: [
            { machine: 'aly', name: longName, severity: 'error', threshold: 0, duration_min: 5, triggered_at: '2026-08-29T18:37:42Z' },
          ],
        }}
      />
    );
    expect(screen.getByTitle(`ALY · ${longName}`)).toBeTruthy();
  });
});
