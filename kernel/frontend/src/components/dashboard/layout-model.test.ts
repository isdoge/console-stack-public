import { describe, expect, it } from 'vitest';
import {
  GRID_COLUMNS,
  buildDefaultLayout,
  defaultWidgetKeys,
  machineWidgetId,
  machineWidgetKey,
  mergeLayout,
  minWidthOf,
  parseLayout,
  serializeLayout,
} from './layout-model';
import type { LayoutItem } from './layout-model';
import type { Machine, MonitorSystem } from '@/lib/types';

const machine = (id: string, role: 'server' | 'node'): Machine =>
  ({ id, label: id, role }) as Machine;
const system = (id: string, machineId?: string): MonitorSystem =>
  ({ id, name: id, machine: machineId }) as MonitorSystem;

const MACHINES = [
  machine('nas', 'server'),
  machine('txy', 'server'),
  machine('aly', 'server'),
  machine('us', 'server'),
  machine('kr', 'node'),
  machine('sg', 'node'),
];

const SOURCE = { machines: MACHINES, systems: [] as MonitorSystem[] };

describe('defaultWidgetKeys', () => {
  it('与静态渲染树同序：关注 → 事件 → 服务器 → 节点 → 运维 → tailnet（巡检在顶栏，不占正文）', () => {
    expect(defaultWidgetKeys(SOURCE)).toEqual([
      'attention',
      'monitor-events',
      'machine-card:nas',
      'machine-card:txy',
      'machine-card:aly',
      'machine-card:us',
      'machine-card:kr',
      'machine-card:sg',
      'ops-status',
      'tailnet',
      'caddy-entry',
    ]);
  });

  it('台账为准：Beszel 挂了（systems 为空）服务器卡照样在', () => {
    const keys = defaultWidgetKeys({ machines: MACHINES, systems: [] });
    expect(keys).toContain('machine-card:nas');
  });

  it('Beszel 里有、台账里没有的系统追加在服务器卡之后，不丢卡', () => {
    const keys = defaultWidgetKeys({
      machines: MACHINES,
      systems: [system('nas', 'nas'), system('BESZEL-ONLY')],
    });
    expect(keys).toContain('machine-card:sys:BESZEL-ONLY');
    // 排在最后一张台账服务器卡之后、第一张节点卡之前
    expect(keys.indexOf('machine-card:sys:BESZEL-ONLY')).toBeGreaterThan(
      keys.indexOf('machine-card:us')
    );
    expect(keys.indexOf('machine-card:sys:BESZEL-ONLY')).toBeLessThan(
      keys.indexOf('machine-card:kr')
    );
  });

  it('role=node 的系统不会被当成未登记服务器重复塞进来', () => {
    const keys = defaultWidgetKeys({
      machines: MACHINES,
      // kr 在台账里是 node，这里给它一个 Beszel 系统记录
      systems: [system('KR', 'kr')],
    });
    expect(keys.filter((k) => k.includes('kr')).length).toBe(1);
    expect(keys).not.toContain('machine-card:sys:KR');
  });

  it('台账为空（/api/machines 未就绪）时只剩固定件 + Beszel 系统', () => {
    expect(defaultWidgetKeys({ machines: [], systems: [system('NAS')] })).toEqual([
      'attention',
      'monitor-events',
      'machine-card:sys:NAS',
      'ops-status',
      'tailnet',
      'caddy-entry',
    ]);
  });
});

describe('buildDefaultLayout', () => {
  const layout = buildDefaultLayout(SOURCE);
  const at = (key: string) => layout.find((i) => i.key === key)!;

  it('整行件独占一行、机器卡一行四张（巡检工具条 D4-2.3 起不在正文区）', () => {
    expect(at('attention')).toEqual({ key: 'attention', x: 0, y: 0, w: 12 });
    expect(at('monitor-events')).toEqual({ key: 'monitor-events', x: 0, y: 12, w: 12 });
    expect(at('machine-card:nas')).toEqual({ key: 'machine-card:nas', x: 0, y: 24, w: 3 });
    expect(at('machine-card:txy')).toEqual({ key: 'machine-card:txy', x: 3, y: 24, w: 3 });
    expect(at('machine-card:us')).toEqual({ key: 'machine-card:us', x: 9, y: 24, w: 3 });
    // 第五张机器卡换行
    expect(at('machine-card:kr')).toEqual({ key: 'machine-card:kr', x: 0, y: 36, w: 3 });
  });

  it('任何一项都不越界（x + w <= 12）', () => {
    for (const item of layout) expect(item.x + item.w).toBeLessThanOrEqual(GRID_COLUMNS);
  });

  it('不产出 h：高度归 sizeToContent', () => {
    for (const item of layout) expect(item).not.toHaveProperty('h');
  });
});

describe('minWidthOf', () => {
  it('机器卡 ≥3、其余 ≥4', () => {
    expect(minWidthOf('machine-card:nas')).toBe(3);
    expect(minWidthOf('monitor-events')).toBe(4);
    expect(minWidthOf('attention')).toBe(4);
  });
});

describe('machineWidgetKey / machineWidgetId', () => {
  it('往返一致', () => {
    expect(machineWidgetId(machineWidgetKey('nas'))).toBe('nas');
  });

  it('非机器卡返回 null', () => {
    expect(machineWidgetId('ops-status')).toBeNull();
  });
});

describe('serializeLayout / parseLayout', () => {
  it('只序列化 key/x/y/w，多余字段（含 h）丢弃', () => {
    const raw = serializeLayout([
      { key: 'attention', x: 0, y: 0, w: 12, h: 9 } as unknown as LayoutItem,
    ]);
    expect(JSON.parse(raw)).toEqual([{ key: 'attention', x: 0, y: 0, w: 12 }]);
  });

  it('往返一致', () => {
    const items = buildDefaultLayout(SOURCE);
    expect(parseLayout(serializeLayout(items))).toEqual(items);
  });

  it('空/坏 JSON → null，不抛错', () => {
    expect(parseLayout(null)).toBeNull();
    expect(parseLayout('')).toBeNull();
    expect(parseLayout('{oops')).toBeNull();
    expect(parseLayout('{"key":"attention"}')).toBeNull(); // 不是数组
    expect(parseLayout('[]')).toBeNull();
  });

  it('单项坏只丢那一项', () => {
    const raw = JSON.stringify([
      { key: 'attention', x: 0, y: 0, w: 12 },
      { key: 'ops-status', x: 0, y: 0, w: 0 }, // w < 1
      { key: 'tailnet', x: 10, y: 0, w: 6 }, // x + w > 12
      { key: '', x: 0, y: 0, w: 3 }, // 空 key
      { key: 'monitor-events', x: 0, y: -1, w: 12 }, // 负 y
      { key: 'inspection', x: 0, y: 1.5, w: 12 }, // 非整数
    ]);
    expect(parseLayout(raw)).toEqual([{ key: 'attention', x: 0, y: 0, w: 12 }]);
  });

  it('全部项都坏 → null（降级成没有 override，而不是空网格）', () => {
    expect(parseLayout(JSON.stringify([{ key: 'attention', x: 99, y: 0, w: 12 }]))).toBeNull();
  });
});

describe('mergeLayout（default ⊕ override）', () => {
  const defaults = buildDefaultLayout(SOURCE);

  it('没有 override 时原样返回 default', () => {
    expect(mergeLayout(defaults, null)).toBe(defaults);
    expect(mergeLayout(defaults, [])).toBe(defaults);
  });

  it('两边都有的 key 以 override 坐标为准', () => {
    const merged = mergeLayout(defaults, [{ key: 'ops-status', x: 0, y: 0, w: 6 }]);
    expect(merged.find((i) => i.key === 'ops-status')).toEqual({
      key: 'ops-status',
      x: 0,
      y: 0,
      w: 6,
    });
  });

  it('override 里的未知 key 忽略（机器下架后留下的孤儿行）', () => {
    const merged = mergeLayout(defaults, [
      { key: 'machine-card:已下架', x: 0, y: 0, w: 3 },
      { key: 'ops-status', x: 6, y: 0, w: 6 },
    ]);
    expect(merged.some((i) => i.key === 'machine-card:已下架')).toBe(false);
    expect(merged.find((i) => i.key === 'ops-status')?.x).toBe(6);
  });

  it('default 里有、override 里没有的 key 追加到网格末尾（新机器自动上场）', () => {
    const override = defaults
      .filter((i) => i.key !== 'machine-card:sg')
      .map((i) => ({ ...i }));
    const merged = mergeLayout(defaults, override);
    expect(merged).toHaveLength(defaults.length);
    const appended = merged[merged.length - 1];
    expect(appended.key).toBe('machine-card:sg');
    const maxKeptY = Math.max(...override.map((i) => i.y));
    expect(appended.y).toBeGreaterThan(maxKeptY);
  });

  it('override 全是未知 key 时退回 default', () => {
    expect(mergeLayout(defaults, [{ key: 'nope', x: 0, y: 0, w: 3 }])).toBe(defaults);
  });

  it('override 里重复 key 只认第一条', () => {
    const merged = mergeLayout(defaults, [
      { key: 'tailnet', x: 0, y: 0, w: 5 },
      { key: 'tailnet', x: 6, y: 9, w: 6 },
    ]);
    expect(merged.find((i) => i.key === 'tailnet')).toEqual({ key: 'tailnet', x: 0, y: 0, w: 5 });
  });

  it('override 的 w 仍受 min w 约束（用户数据不能靠它守约束）', () => {
    const merged = mergeLayout(defaults, [
      { key: 'machine-card:nas', x: 0, y: 0, w: 1 },
      { key: 'monitor-events', x: 0, y: 12, w: 2 },
    ]);
    expect(merged.find((i) => i.key === 'machine-card:nas')?.w).toBe(3);
    expect(merged.find((i) => i.key === 'monitor-events')?.w).toBe(4);
  });

  it('合并结果不越界', () => {
    const merged = mergeLayout(defaults, [{ key: 'attention', x: 8, y: 0, w: 4 }]);
    for (const item of merged) expect(item.x + item.w).toBeLessThanOrEqual(GRID_COLUMNS);
  });
});

// D1' 起 localStorage 读写收敛到 lib/dashboard-layout-store.ts（升库 SQLite v9），
// 本文件只守纯模型逻辑；存储回路（含 legacy 迁移）见 dashboard-layout-store.test.ts。
