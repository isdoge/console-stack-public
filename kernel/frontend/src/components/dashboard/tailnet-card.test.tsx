import { displayTime } from '@/lib/display-time';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import {
  keyExpiryText,
  keyExpiryTone,
  onlineDotTone,
  onlineSummary,
  TailnetCard,
} from './tailnet-card';
import { TAILNET_FIELDS_STORAGE_KEY } from './tailnet-fields';
import type { TailnetStatus } from '@/lib/types';

vi.mock('@/lib/api', () => ({
  getTailnet: vi.fn(),
}));

const machine = (overrides: Record<string, unknown>) => ({
  machine_id: 'nas',
  hostname: 'nas',
  online: true,
  key_expiry_days: 120,
  last_seen: null,
  client_version: '1.78.0',
  os: null,
  update_available: null,
  authorized: null,
  ...overrides,
});

beforeEach(() => {
  localStorage.removeItem(TAILNET_FIELDS_STORAGE_KEY);
});

describe('keyExpiryText', () => {
  test('null → 不过期；负数 → 已过期；非负 → 剩 N 天', () => {
    expect(keyExpiryText(null)).toBe('不过期');
    expect(keyExpiryText(-3)).toBe('已过期 3 天');
    expect(keyExpiryText(0)).toBe('剩 0 天');
    expect(keyExpiryText(7)).toBe('剩 7 天');
    expect(keyExpiryText(120)).toBe('剩 120 天');
  });
});

describe('keyExpiryTone（与 attention 7 天窗口同口径）', () => {
  test('已过期红、≤7 天警告、>7 天弱化、null 弱化', () => {
    expect(keyExpiryTone(-1)).toBe('text-signal-5');
    expect(keyExpiryTone(7)).toBe('text-signal-4');
    expect(keyExpiryTone(8)).toBe('text-muted-foreground');
    expect(keyExpiryTone(null)).toBe('text-muted-foreground');
  });
});

describe('onlineDotTone', () => {
  test('true 绿 / false 红 / null 未知灰', () => {
    expect(onlineDotTone(true)).toBe('bg-signal-1');
    expect(onlineDotTone(false)).toBe('bg-signal-5');
    expect(onlineDotTone(null)).toBe('bg-muted-foreground');
  });
});

describe('onlineSummary', () => {
  test('统计在线数/总数', () => {
    const machines = [
      machine({ machine_id: 'a', online: true }),
      machine({ machine_id: 'b', online: false }),
      machine({ machine_id: 'c', online: null }),
    ];
    expect(onlineSummary(machines as TailnetStatus['machines'])).toBe('1/3 在线');
  });
});

describe('TailnetCard', () => {
  test('源过期后隐藏旧机器与到期值，保留独立源采集时间', () => {
    const status: TailnetStatus = {
      available: true, sampled_at: '2026-09-08T12:00:00Z', error: null,
      source_sampled_at: '2026-09-08T11:45:00.125Z',
      machines: [machine({ key_expiry_days: 3 })] as TailnetStatus['machines'],
    };
    const { rerender } = render(<TailnetCard status={status} />);
    expect(screen.getByText(displayTime('2026-09-08T11:45:00.125Z').text)).toBeTruthy();
    expect(screen.getByText('1/1 在线')).toBeTruthy();
    expect(screen.getByText('剩 3 天')).toBeTruthy();
    rerender(<TailnetCard status={{
      ...status, available: false, error: 'Tailnet 数据已过期（超过 15 分钟未更新）',
    }} />);
    expect(screen.queryByText('nas')).toBeNull();
    expect(screen.queryByText('1/1 在线')).toBeNull();
    expect(screen.queryByText('剩 3 天')).toBeNull();
    expect(screen.getByText(/数据已过期/)).toBeTruthy();
    expect(screen.getByText(displayTime('2026-09-08T11:45:00.125Z').text)).toBeTruthy();
  });

  test('available：渲染机器 chip 与在线汇总', () => {
    const status: TailnetStatus = {
      available: true,
      sampled_at: '2026-09-06T12:00:00Z',
      error: null,
      machines: [
        machine({ machine_id: 'nas', key_expiry_days: 120 }),
        machine({ machine_id: 'txy', online: false, key_expiry_days: 3 }),
        machine({ machine_id: 'aly', hostname: null, online: null, key_expiry_days: null }),
      ] as TailnetStatus['machines'],
    };
    render(<TailnetCard status={status} />);
    expect(screen.getByText('Tailnet 组网')).toBeTruthy();
    expect(screen.getByText('1/3 在线')).toBeTruthy();
    expect(screen.getByText('nas')).toBeTruthy();
    expect(screen.getByText('剩 3 天')).toBeTruthy();
    expect(screen.getByText('不过期')).toBeTruthy();
    expect(screen.queryByText(/源采集：/)).toBeNull();
  });

  test('unavailable：显式降级文案，不渲染机器格子', () => {
    const status: TailnetStatus = {
      available: false,
      sampled_at: '2026-09-06T12:00:00Z',
      error: 'tailscaled socket 不存在（宿主演化或挂载缺失）',
      machines: [],
    };
    render(<TailnetCard status={status} />);
    expect(screen.getByText(/Tailnet 数据不可用/)).toBeTruthy();
    expect(screen.getByText(/socket 不存在/)).toBeTruthy();
  });
});

describe("TailnetCard D4 三字段与 fields 白名单（C2 增强）", () => {
  const status: TailnetStatus = {
    available: true,
    sampled_at: '2026-09-06T12:00:00Z',
    error: null,
    machines: [
      machine({ os: 'linux', update_available: true, authorized: false }),
      machine({ machine_id: 'txy', os: null, update_available: false, authorized: true }),
    ] as TailnetStatus['machines'],
  };

  test('默认全显：OS 文本与版本更新图标出现；authorized=false 进 tooltip 不占列', () => {
    render(<TailnetCard status={status} />);
    expect(screen.getByText('linux')).toBeTruthy();
    // 版本更新图标带 sr-only 文案，可断言
    expect(screen.getAllByText('有新版本').length).toBe(1);
    // 未授权不占列，只在 tooltip 里（title 挂在 chip 外层 div 上）
    expect(screen.queryByText('未授权')).toBeNull();
    const nasChip = screen.getByText('nas').closest('div') as HTMLElement;
    const txyChip = screen.getByText('txy').closest('div') as HTMLElement;
    expect(nasChip.title).toContain('未授权');
    expect(txyChip.title).not.toContain('未授权');
    expect(nasChip.title).toContain('linux');
  });

  test('白名单只留 key 到期：OS 与版本更新列消失', () => {
    render(<TailnetCard status={status} initialFields={['expiry']} />);
    expect(screen.queryByText('linux')).toBeNull();
    expect(screen.queryByText('有新版本')).toBeNull();
    // 两台机器的 key_expiry_days 都取 fixture 默认 120
    expect(screen.getAllByText('剩 120 天').length).toBe(2);
  });

  test('菜单切换字段：取消「系统」后写 localStorage 且列消失', () => {
    render(<TailnetCard status={status} />);
    expect(screen.getByText('linux')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '显示字段' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /系统/ }));

    expect(screen.queryByText('linux')).toBeNull();
    expect(JSON.parse(localStorage.getItem(TAILNET_FIELDS_STORAGE_KEY)!)).toEqual([
      'update',
      'expiry',
    ]);
  });

  test('localStorage 坏数据 → 降级默认全显，不炸卡', () => {
    localStorage.setItem(TAILNET_FIELDS_STORAGE_KEY, '{不是数组');
    render(<TailnetCard status={status} />);
    expect(screen.getByText('linux')).toBeTruthy();
    expect(screen.getByText('有新版本')).toBeTruthy();
  });

  test('显式空白名单 = 最小视图：只剩在线点与机器 id', () => {
    render(<TailnetCard status={status} initialFields={[]} />);
    expect(screen.queryByText('linux')).toBeNull();
    expect(screen.queryByText('有新版本')).toBeNull();
    expect(screen.queryByText('剩 120 天')).toBeNull();
    expect(screen.getByText('nas')).toBeTruthy();
    expect(screen.getByText('txy')).toBeTruthy();
  });
});
