import { beforeEach, describe, expect, test, vi } from 'vitest';
import {
  EMPTY_LAYOUT_STATE,
  LEGACY_HIDDEN_STORAGE_KEY,
  LEGACY_LAYOUT_STORAGE_KEY,
  clearLegacyState,
  itemsFromState,
  loadDashboardLayout,
  persistDashboardLayout,
  readLegacyState,
  stateFromItems,
  statesEqual,
} from './dashboard-layout-store';
import type { LayoutItem } from '@/components/dashboard/layout-model';
import * as api from '@/lib/api';
import type { DashboardLayoutItem } from '@/lib/types';

vi.mock('@/lib/api', () => ({
  getDashboardLayout: vi.fn(),
  saveDashboardLayout: vi.fn(),
}));

const mockedGet = vi.mocked(api.getDashboardLayout);
const mockedSave = vi.mocked(api.saveDashboardLayout);

const item = (key: string, x: number, y: number, w: number, hidden = false): DashboardLayoutItem => ({
  widget_key: key,
  x,
  y,
  w,
  hidden,
});

const defaults: LayoutItem[] = [
  { key: 'attention', x: 0, y: 0, w: 12 },
  { key: 'machine-card:nas', x: 0, y: 12, w: 3 },
  { key: 'machine-card:kr', x: 3, y: 12, w: 3 },
  { key: 'tailnet', x: 0, y: 24, w: 12 },
];

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('stateFromItems（GET 行 → 会话状态）', () => {
  test('空行集 → 没有 override、没有隐藏（走静态渲染树）', () => {
    expect(stateFromItems([])).toEqual(EMPTY_LAYOUT_STATE);
  });

  test('hidden=0 的行进 layoutOverride，hidden=1 的行进 hidden 集合', () => {
    const state = stateFromItems([
      item('machine-card:nas', 6, 0, 3),
      item('tailnet', 0, 24, 12, true),
    ]);
    expect(state.layoutOverride).toEqual([
      { key: 'machine-card:nas', x: 6, y: 0, w: 3 },
      { key: 'tailnet', x: 0, y: 24, w: 12 },
    ]);
    expect(state.hidden).toEqual(['tailnet']);
  });

  test('hidden 行的坐标也进 layoutOverride（找回时按原位恢复；全 hidden 时 override 非空但仍走网格路径）', () => {
    const state = stateFromItems([item('tailnet', 0, 0, 12, true)]);
    expect(state.layoutOverride).toEqual([{ key: 'tailnet', x: 0, y: 0, w: 12 }]);
    expect(state.hidden).toEqual(['tailnet']);
  });
});

describe('itemsFromState（会话状态 → PUT 行集）', () => {
  test('挪过的件带 override 坐标；没挪过的 hidden 件补默认坐标', () => {
    const items = itemsFromState(
      {
        layoutOverride: [{ key: 'machine-card:nas', x: 6, y: 0, w: 3 }],
        hidden: ['tailnet'],
      },
      defaults
    );
    const byKey = new Map(items.map((it) => [it.widget_key, it]));
    expect(byKey.get('machine-card:nas')).toEqual({
      widget_key: 'machine-card:nas',
      x: 6,
      y: 0,
      w: 3,
      hidden: false,
    });
    // tailnet 从未挪过 → 用默认布局的坐标补上（表列 NOT NULL，必须有值）
    expect(byKey.get('tailnet')).toEqual({
      widget_key: 'tailnet',
      x: 0,
      y: 24,
      w: 12,
      hidden: true,
    });
    expect(items).toHaveLength(2);
  });

  test('拖过再隐藏的件：坐标保留 override 值（找回后恢复原位）', () => {
    const items = itemsFromState(
      {
        layoutOverride: [{ key: 'machine-card:nas', x: 6, y: 0, w: 3 }],
        hidden: ['machine-card:nas'],
      },
      defaults
    );
    expect(items[0]).toEqual({
      widget_key: 'machine-card:nas',
      x: 6,
      y: 0,
      w: 3,
      hidden: true,
    });
  });

  test('默认布局里已不存在的孤儿 key 丢弃（PUT 整体替换顺带清理）', () => {
    const items = itemsFromState(
      {
        layoutOverride: [{ key: 'machine-card:已下架', x: 0, y: 0, w: 3 }],
        hidden: ['machine-card:已下架'],
      },
      defaults
    );
    expect(items).toEqual([]);
  });

  test('空状态 → 空 items（PUT 空 = 回 default⊕∅）', () => {
    expect(itemsFromState(EMPTY_LAYOUT_STATE, defaults)).toEqual([]);
  });
});

describe('statesEqual（迁移前比对，键序无关）', () => {
  test('同一内容不同顺序视为相等', () => {
    const a: typeof EMPTY_LAYOUT_STATE = {
      layoutOverride: [
        { key: 'attention', x: 0, y: 0, w: 12 },
        { key: 'tailnet', x: 0, y: 12, w: 6 },
      ],
      hidden: ['ops-status'],
    };
    const b: typeof EMPTY_LAYOUT_STATE = {
      layoutOverride: [
        { key: 'tailnet', x: 0, y: 12, w: 6 },
        { key: 'attention', x: 0, y: 0, w: 12 },
      ],
      hidden: ['ops-status'],
    };
    expect(statesEqual(a, b)).toBe(true);
  });

  test('坐标 / 隐藏集合任一不同即不等', () => {
    const base: typeof EMPTY_LAYOUT_STATE = {
      layoutOverride: [{ key: 'tailnet', x: 0, y: 12, w: 6 }],
      hidden: [],
    };
    expect(
      statesEqual(base, { layoutOverride: [{ key: 'tailnet', x: 1, y: 12, w: 6 }], hidden: [] })
    ).toBe(false);
    expect(statesEqual(base, { layoutOverride: base.layoutOverride, hidden: ['x'] })).toBe(false);
    expect(statesEqual(base, EMPTY_LAYOUT_STATE)).toBe(false);
  });
});

describe('legacy localStorage 读写（仅迁移路径碰）', () => {
  test('两个旧键都在 → 读出完整状态', () => {
    localStorage.setItem(
      LEGACY_LAYOUT_STORAGE_KEY,
      JSON.stringify([{ key: 'machine-card:nas', x: 6, y: 0, w: 3 }])
    );
    localStorage.setItem(LEGACY_HIDDEN_STORAGE_KEY, JSON.stringify(['tailnet']));
    expect(readLegacyState()).toEqual({
      layoutOverride: [{ key: 'machine-card:nas', x: 6, y: 0, w: 3 }],
      hidden: ['tailnet'],
    });
  });

  test('只有 hidden 键也能读出（D3「只隐藏没挪过」的形态）', () => {
    localStorage.setItem(LEGACY_HIDDEN_STORAGE_KEY, JSON.stringify(['tailnet']));
    expect(readLegacyState()).toEqual({ layoutOverride: null, hidden: ['tailnet'] });
  });

  test('两个键都 absent → null（无需迁移）', () => {
    expect(readLegacyState()).toBeNull();
  });

  test('坏 JSON 降级：layout 坏 → null 坐标 + hidden 照读', () => {
    localStorage.setItem(LEGACY_LAYOUT_STORAGE_KEY, '{不是数组');
    localStorage.setItem(LEGACY_HIDDEN_STORAGE_KEY, JSON.stringify(['tailnet']));
    expect(readLegacyState()).toEqual({ layoutOverride: null, hidden: ['tailnet'] });
  });

  test('localStorage 抛异常（隐私模式）→ null / 静默', () => {
    const original = window.localStorage;
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new Error('denied');
    });
    try {
      expect(readLegacyState()).toBeNull();
      expect(() => clearLegacyState()).not.toThrow();
    } finally {
      Object.defineProperty(window, 'localStorage', { value: original, configurable: true });
    }
  });

  test('clearLegacyState 清掉两个旧键', () => {
    localStorage.setItem(LEGACY_LAYOUT_STORAGE_KEY, '[]');
    localStorage.setItem(LEGACY_HIDDEN_STORAGE_KEY, '[]');
    clearLegacyState();
    expect(localStorage.getItem(LEGACY_LAYOUT_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(LEGACY_HIDDEN_STORAGE_KEY)).toBeNull();
  });
});

describe('loadDashboardLayout', () => {
  test('成功 → DB 行转会话状态，可写', async () => {
    mockedGet.mockResolvedValue({
      items: [item('machine-card:nas', 6, 0, 3)],
      updated_at: '2026-09-06T00:00:00Z',
    });
    const result = await loadDashboardLayout();
    expect(result.readOnly).toBe(false);
    expect(result.error).toBeNull();
    expect(result.state.layoutOverride).toEqual([{ key: 'machine-card:nas', x: 6, y: 0, w: 3 }]);
  });

  test('失败 → 显式只读降级 + 报错文案，状态回默认（不回退 localStorage）', async () => {
    localStorage.setItem(
      LEGACY_LAYOUT_STORAGE_KEY,
      JSON.stringify([{ key: 'attention', x: 0, y: 0, w: 12 }])
    );
    mockedGet.mockRejectedValue(new Error('boom'));
    const result = await loadDashboardLayout();
    expect(result.readOnly).toBe(true);
    expect(result.error).toBeTruthy();
    expect(result.state).toEqual(EMPTY_LAYOUT_STATE);
    // 旧键原样保留，不在这条路径上清
    expect(localStorage.getItem(LEGACY_LAYOUT_STORAGE_KEY)).toBeTruthy();
  });
});

describe('persistDashboardLayout', () => {
  test('PUT 的行集来自 itemsFromState（hidden 补默认坐标、孤儿丢弃）', async () => {
    mockedSave.mockResolvedValue({ items: [], updated_at: '2026-09-06T00:00:00Z' });
    await persistDashboardLayout(
      { layoutOverride: null, hidden: ['tailnet'] },
      defaults
    );
    expect(mockedSave).toHaveBeenCalledWith([
      { widget_key: 'tailnet', x: 0, y: 24, w: 12, hidden: true },
    ]);
  });

  test('失败向上抛（调用方决定留在编辑态重试）', async () => {
    mockedSave.mockRejectedValue(new Error('boom'));
    await expect(
      persistDashboardLayout(EMPTY_LAYOUT_STATE, defaults)
    ).rejects.toThrow('boom');
  });
});
