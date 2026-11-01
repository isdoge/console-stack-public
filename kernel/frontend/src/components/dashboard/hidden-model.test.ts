import { describe, expect, test } from 'vitest';
import { parseHidden, serializeHidden, widgetLabel } from './hidden-model';
import type { Machine, MonitorSystem } from '@/lib/types';

const machines: Machine[] = [
  { id: 'nas', label: 'NAS', role: 'server' },
  { id: 'kr', label: '韩国节点', role: 'node' },
  { id: 'noname', label: '', role: 'server' },
];
const systems: MonitorSystem[] = [
  { id: 'sys-1', name: 'Beszel 专属机', status: 'up' },
  { id: 'sys-2', name: '', status: 'up' },
];

describe('parseHidden（C8/D3）', () => {
  test('正常数组原样收下', () => {
    expect(parseHidden('["attention","ops-status"]')).toEqual(['attention', 'ops-status']);
  });

  test('坏 JSON / 非数组 / 空值一律降级成空集合', () => {
    expect(parseHidden('{不是数组')).toEqual([]);
    expect(parseHidden('{"attention":true}')).toEqual([]);
    expect(parseHidden('null')).toEqual([]);
    expect(parseHidden(null)).toEqual([]);
    expect(parseHidden(undefined)).toEqual([]);
    expect(parseHidden('')).toEqual([]);
  });

  test('丢掉非字符串与空串项，其余保留', () => {
    expect(parseHidden('["attention",1,null,"",{"a":1},"tailnet"]')).toEqual([
      'attention',
      'tailnet',
    ]);
  });

  test('重复项去重', () => {
    expect(parseHidden('["attention","attention","tailnet"]')).toEqual(['attention', 'tailnet']);
  });
});

describe('serializeHidden（C8/D3）', () => {
  test('去重后写出数组', () => {
    expect(serializeHidden(['a', 'b', 'a'])).toBe('["a","b"]');
  });
});

// D1' 起 localStorage 读写收敛到 lib/dashboard-layout-store.ts（升库 SQLite v9），
// hidden 回路（含 legacy 迁移）见 dashboard-layout-store.test.ts。

describe('widgetLabel（C8/D3）', () => {
  const source = { machines, systems };

  test('固定件用中文名', () => {
    expect(widgetLabel('attention', source)).toBe('今日关注');
    expect(widgetLabel('monitor-events', source)).toBe('监控事件');
    expect(widgetLabel('ops-status', source)).toBe('运维状态');
    expect(widgetLabel('tailnet', source)).toBe('Tailscale 网络');
  });

  test('机器卡用台账 label', () => {
    expect(widgetLabel('machine-card:nas', source)).toBe('NAS');
    expect(widgetLabel('machine-card:kr', source)).toBe('韩国节点');
  });

  test('台账没有、只在 Beszel 里的系统用 system name', () => {
    expect(widgetLabel('machine-card:sys:sys-1', source)).toBe('Beszel 专属机');
  });

  test('查不到 / 名字为空一律退回 id，不产出空名条目', () => {
    expect(widgetLabel('machine-card:ghost', source)).toBe('ghost');
    expect(widgetLabel('machine-card:noname', source)).toBe('noname');
    expect(widgetLabel('machine-card:sys:sys-2', source)).toBe('sys-2');
    expect(widgetLabel('machine-card:sys:nope', source)).toBe('nope');
    expect(widgetLabel('未知件', source)).toBe('未知件');
  });
});
