import { displayTime } from '@/lib/display-time';
import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ObservabilityMachine, formatBytes } from './observability-machine';
import type { Machine, ObservabilityPayload } from '@/lib/types';

const BASE: ObservabilityPayload = {
  ok: true,
  updated_at: '2026-08-30T00:00:00Z',
  probe_interval: 600,
  source_errors: [],
  machine_links: {},
  active_alerts: [],
  recent_events: [],
  systemd_services: [],
  smart_devices: [],
};

const US: Machine = {
  id: 'us',
  label: 'Oracle',
  role: 'server',
  actions: [],
  systemd_units: ['sing-box'],
};

const KR: Machine = {
  id: 'kr',
  label: '光帆 KR',
  role: 'node',
  actions: [{ key: 'restart-singbox', label: '重启 sing-box' }],
  systemd_units: ['sing-box'],
};

const NAS: Machine = { id: 'nas', label: 'NAS', role: 'server', actions: [] };

describe('ObservabilityMachine（§4.2 / D3）', () => {
  test('null 或无本机数据时不渲染', () => {
    const { container } = render(<ObservabilityMachine machine={US} data={null} />);
    expect(container.firstChild).toBeNull();
    const { container: c2 } = render(<ObservabilityMachine machine={US} data={BASE} />);
    expect(c2.firstChild).toBeNull();
  });

  test('PG 空列表明确提示能力未覆盖，并尊重块级显隐', () => {
    const data: ObservabilityPayload = {
      ...BASE,
      source: 'console_pg',
      capabilities: { systemd: 'unsupported', smart: 'unsupported' },
    };
    const { rerender, container } = render(<ObservabilityMachine machine={US} data={data} />);
    expect(screen.getByText(/当前 Console PG 未覆盖：systemd 服务、SMART\/mdraid/)).toBeTruthy();
    expect(screen.getByText(/空列表不代表检查通过/)).toBeTruthy();
    rerender(<ObservabilityMachine machine={US} data={data} visible={{ systemd: false, storage: false }} />);
    expect(container.firstChild).toBeNull();
  });

  test('只显示本机 systemd 白名单记录，failed 用 destructive', () => {
    render(
      <ObservabilityMachine
        machine={KR}
        data={{
          ...BASE,
          systemd_services: [
            { machine: 'kr', unit: 'sing-box', state: 'active', sub_state: 'running', cpu_pct: 0, cpu_peak_pct: 0, memory_bytes: 13656064, memory_peak_bytes: 33103872, updated_at: '2026-08-29T19:16:21Z' },
            { machine: 'sg', unit: 'sing-box', state: 'active', sub_state: 'running', cpu_pct: 0, cpu_peak_pct: 0, memory_bytes: 1, memory_peak_bytes: 1, updated_at: '2026-08-29T19:16:21Z' },
          ],
        }}
      />
    );
    // 只渲染 kr 的 sing-box，sg 的记录不进视图
    expect(screen.getByText('sing-box')).toBeTruthy();
    expect(screen.getByText(/active · running/)).toBeTruthy();
    expect(screen.getAllByText('sing-box')).toHaveLength(1);
  });

  test('failed unit 红态：state 或 sub_state 任一 failed 都标红', () => {
    render(
      <ObservabilityMachine
        machine={KR}
        data={{
          ...BASE,
          systemd_services: [
            { machine: 'kr', unit: 'sing-box', state: 'failed', sub_state: 'failed', cpu_pct: 0, cpu_peak_pct: 0, memory_bytes: 0, memory_peak_bytes: 0, updated_at: '2026-08-29T19:16:21Z' },
          ],
        }}
      />
    );
    const badge = screen.getByText(/failed · failed/);
    expect(badge.className).toContain('text-destructive');
  });

  test('存储区只在本机有设备时显示；PASSED 绿、非 PASSED 红', () => {
    render(
      <ObservabilityMachine
        machine={NAS}
        data={{
          ...BASE,
          smart_devices: [
            { machine: 'nas', device: '/dev/md0', type: 'mdraid', model: null, state: 'PASSED', capacity_bytes: 443255095296, temperature_c: null, power_on_hours: null, cycles: null, updated_at: '2026-08-29T18:41:25Z' },
            { machine: 'nas', device: '/dev/md1', type: 'mdraid', model: null, state: 'REBUILDING', capacity_bytes: 500000000000, temperature_c: null, power_on_hours: null, cycles: null, updated_at: '2026-08-29T18:41:25Z' },
          ],
        }}
      />
    );
    expect(screen.getByText('/dev/md0')).toBeTruthy();
    expect(screen.getByText('/dev/md1')).toBeTruthy();
    const passed = screen.getByText('PASSED');
    expect(passed.className).toContain('emerald');
    const bad = screen.getByText('REBUILDING');
    expect(bad.className).toContain('text-destructive');
  });

  test('0/null 温度和小时显示 —，不显示 0°C / 0 小时', () => {
    render(
      <ObservabilityMachine
        machine={NAS}
        data={{
          ...BASE,
          smart_devices: [
            { machine: 'nas', device: '/dev/md0', type: 'mdraid', model: null, state: 'PASSED', capacity_bytes: 443255095296, temperature_c: 0, power_on_hours: 0, cycles: null, updated_at: '2026-08-29T18:41:25Z' },
          ],
        }}
      />
    );
    expect(screen.getByText('温度 —')).toBeTruthy();
    expect(screen.getByText('通电 —')).toBeTruthy();
    expect(screen.getByText('循环 —')).toBeTruthy();
    expect(screen.queryByText('温度 0°C')).toBeNull();
  });

  test('本机事件最多 10 条且只取本机', () => {
    const events = Array.from({ length: 12 }).map((_, i) => ({
      machine: i % 2 === 0 ? 'us' : 'kr',
      name: `事件${i}`,
      severity: 'warn' as const,
      threshold: 0,
      opened_at: `2026-08-29T1${i}:00:00Z`,
      resolved_at: null,
      state: 'active',
      duration_sec: null,
    }));
    render(<ObservabilityMachine machine={US} data={{ ...BASE, recent_events: events }} />);
    // us 有 6 条（i=0,2,4,6,8,10），kr 的 6 条不进本机视图
    expect(screen.getAllByText(/事件\d+/).length).toBe(6);
    expect(screen.queryByText('事件1')).toBeNull();
  });

  test('US 显示 sing-box 也绝不出现 restart；本组件是纯展示，KR 也不加动作按钮', () => {
    render(
      <ObservabilityMachine
        machine={US}
        data={{
          ...BASE,
          systemd_services: [
            { machine: 'us', unit: 'sing-box', state: 'failed', sub_state: 'failed', cpu_pct: 0, cpu_peak_pct: 0, memory_bytes: 0, memory_peak_bytes: 0, updated_at: '2026-08-29T19:16:21Z' },
          ],
        }}
      />
    );
    expect(screen.getByText('sing-box')).toBeTruthy();
    expect(screen.queryByText('重启 sing-box')).toBeNull();
    // 卡片上允许出现的按钮只有折叠开关（aria-expanded），绝无动作按钮
    const buttons = screen.getAllByRole('button');
    expect(buttons.length).toBeGreaterThan(0);
    buttons.forEach((b) => expect(b.getAttribute('aria-expanded') != null).toBe(true));

    const { container } = render(
      <ObservabilityMachine
        machine={KR}
        data={{
          ...BASE,
          systemd_services: [
            { machine: 'kr', unit: 'sing-box', state: 'active', sub_state: 'running', cpu_pct: 0, cpu_peak_pct: 0, memory_bytes: 0, memory_peak_bytes: 0, updated_at: '2026-08-29T19:16:21Z' },
          ],
        }}
      />
    );
    // KR 卡同样只许折叠开关，不许出现任何动作按钮
    container.querySelectorAll('button').forEach((b) => {
      expect(b.getAttribute('aria-expanded') != null).toBe(true);
    });
    expect(screen.queryByText('重启')).toBeNull();
  });

  test('formatBytes 帮助函数', () => {
    expect(formatBytes(null)).toBe('—');
    expect(formatBytes(13656064)).toBe('13.0 MB');
    expect(formatBytes(443255095296)).toBe('412.8 GB');
  });

  test('375px 友好：systemd/存储区用 flex/grid 换行，不写固定宽 <table>', () => {
    const { container } = render(
      <ObservabilityMachine
        machine={NAS}
        data={{
          ...BASE,
          smart_devices: [
            { machine: 'nas', device: '/dev/md0', type: 'mdraid', model: null, state: 'PASSED', capacity_bytes: 443255095296, temperature_c: null, power_on_hours: null, cycles: null, updated_at: '2026-08-29T18:41:25Z' },
          ],
        }}
      />
    );
    expect(container.querySelector('table')).toBeNull();
  });

  test('长事件名截断且有 title', () => {
    const longName = 'Status'.repeat(30);
    render(
      <ObservabilityMachine
        machine={US}
        data={{
          ...BASE,
          recent_events: [
            { machine: 'us', name: longName, severity: 'warn', threshold: 0, opened_at: '2026-08-29T10:00:00Z', resolved_at: null, state: 'active', duration_sec: null },
          ],
        }}
      />
    );
    expect(screen.getByTitle(`${longName} · ${displayTime('2026-08-29T10:00:00Z').title}`, { normalizer: (value) => value })).toBeTruthy();
  });

  test('source_errors 非空时明确标注全局采集故障，避免误读为本机异常', () => {
    const { container } = render(
      <ObservabilityMachine
        machine={US}
        data={{
          ...BASE,
          source_errors: [{ source: 'systemd_services', message: '采集超时' }],
        }}
      />
    );
    expect(container.firstChild).not.toBeNull();
    expect(screen.getByText('Console 全局监控数据部分不可用')).toBeTruthy();
    expect(screen.getByText(/systemd_services: 采集超时/)).toBeTruthy();
  });
});
