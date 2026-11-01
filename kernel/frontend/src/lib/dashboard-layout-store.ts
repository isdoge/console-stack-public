import { parseHidden, serializeHidden } from '@/components/dashboard/hidden-model';
import { parseLayout, serializeLayout } from '@/components/dashboard/layout-model';
import type { LayoutItem, WidgetKey } from '@/components/dashboard/layout-model';
import { getDashboardLayout, saveDashboardLayout } from '@/lib/api';
import type { DashboardLayoutItem } from '@/lib/types';

/**
 * C8 / D1'：总控台布局的存储层（localStorage → SQLite v9）。
 *
 * **读写收敛到本模块**：D3 时代散在 layout-model / hidden-model / dashboard.tsx 里的
 * localStorage 直写全部收拢到这里——此后布局的唯一权威是 DB
 * （`GET/PUT /api/dashboard-layout`），本模块只保留两件事碰旧键：
 *
 * 1. **一次性迁移**：启动时检测 localStorage 里 D3 的两个旧键
 *    （`console.dashboard.layout` / `console.dashboard.hidden`）→ PUT 到 DB →
 *    成功后清除旧键；失败保留旧键待下次启动重试（不清空，防丢用户布局）。
 * 2. legacy 读取只发生在迁移里；迁移完成后旧键不再有任何读写。
 *
 * **降级**：GET 失败 → `readOnly: true`，总控台按默认布局只读显示并明确报错，
 * **不回退 localStorage 当写入目标**（避免 DB/localStorage 双源）。
 * **多标签页**：不做轮询也不监听 storage 事件——其他标签页刷新后生效。
 *
 * 表行与会话状态的映射（与 D3 两个独立键的语义对齐）：
 * - `hidden=0` 的行 → `layoutOverride`（稀疏位置 override）；
 * - `hidden=1` 的行 → `hidden` 集合，**坐标同时并入 `layoutOverride`**——
 *   D3 的「拖过再隐藏、找回后位置不变」依赖 override 里仍留着那份坐标；
 *   从未挪过就被隐藏的 widget，保存时补默认坐标（见 `itemsFromState`），
 *   找回后仍落在默认位（与 D3 的差异仅是此后留在网格路径，视觉一致）。
 */

/** D1' 升库前的旧键；迁移成功后清除。键名不变（configuration.md §7 已登记）。 */
export const LEGACY_LAYOUT_STORAGE_KEY = 'console.dashboard.layout';
export const LEGACY_HIDDEN_STORAGE_KEY = 'console.dashboard.hidden';

/** 编辑会话的操作对象：与 D3 的两个状态变量同构。 */
export interface DashboardLayoutState {
  layoutOverride: LayoutItem[] | null;
  hidden: WidgetKey[];
}

export const EMPTY_LAYOUT_STATE: DashboardLayoutState = {
  layoutOverride: null,
  hidden: [],
};

export interface LoadResult {
  state: DashboardLayoutState;
  /** true = 布局服务不可用，只读降级（编辑入口不开放、保存不可用） */
  readOnly: boolean;
  /** 非 null = 给用户看的降级原因 */
  error: string | null;
}

// ---- 纯函数：行 ↔ 会话状态 ----------------------------------------------

/** GET 响应行 → 会话状态。hidden 行的坐标并入 layoutOverride（见文件头注释）。 */
export function stateFromItems(items: DashboardLayoutItem[]): DashboardLayoutState {
  const layoutOverride: LayoutItem[] = [];
  const hidden: WidgetKey[] = [];
  for (const item of items) {
    if (!item.widget_key) continue;
    if (item.hidden) {
      hidden.push(item.widget_key);
    }
    // 隐藏行坐标也进 override：找回时按原位恢复（hidden=1 的行坐标本就保留）
    layoutOverride.push({ key: item.widget_key, x: item.x, y: item.y, w: item.w });
  }
  return {
    layoutOverride: layoutOverride.length > 0 ? layoutOverride : null,
    hidden,
  };
}

/**
 * 会话状态 + 当前默认布局 → PUT 行集。
 * - 从未挪过就被隐藏的 widget 补默认坐标（表列 x/y/w NOT NULL，必须有值）；
 * - 默认布局里已不存在的 key（机器下架 / D4 撤下的 widget）丢弃——
 *   PUT 是整体替换，孤儿行顺带从 DB 清掉，也避免被后端白名单 400 卡死保存。
 */
export function itemsFromState(
  state: DashboardLayoutState,
  defaults: LayoutItem[]
): DashboardLayoutItem[] {
  const defaultByKey = new Map(defaults.map((d) => [d.key, d]));
  const coordsByKey = new Map<WidgetKey, { x: number; y: number; w: number }>();
  for (const item of state.layoutOverride ?? []) {
    coordsByKey.set(item.key, { x: item.x, y: item.y, w: item.w });
  }
  const hiddenSet = new Set(state.hidden);
  const items: DashboardLayoutItem[] = [];
  for (const key of new Set<WidgetKey>([...coordsByKey.keys(), ...hiddenSet])) {
    const def = defaultByKey.get(key);
    if (!def) continue;
    const coords = coordsByKey.get(key) ?? { x: def.x, y: def.y, w: def.w };
    items.push({
      widget_key: key,
      x: coords.x,
      y: coords.y,
      w: coords.w,
      hidden: hiddenSet.has(key),
    });
  }
  return items;
}

/** 语义等价（键序无关）：迁移前比对用，避免对内容相同的 DB 做无谓覆盖。 */
export function statesEqual(a: DashboardLayoutState, b: DashboardLayoutState): boolean {
  const norm = (s: DashboardLayoutState): string => {
    const coords = (s.layoutOverride ?? [])
      .map((it) => [it.key, it.x, it.y, it.w] as const)
      .sort((x, y) => (x[0] < y[0] ? -1 : 1));
    return `${serializeLayout(coords.map(([key, x, y, w]) => ({ key, x, y, w })))}|${serializeHidden(s.hidden)}`;
  };
  return norm(a) === norm(b);
}

// ---- legacy localStorage（仅迁移路径碰） ---------------------------------

/** 读 D3 的两个旧键；两个都 absent（或读不出来）返回 null。 */
export function readLegacyState(): DashboardLayoutState | null {
  let rawLayout: string | null;
  let rawHidden: string | null;
  try {
    rawLayout = localStorage.getItem(LEGACY_LAYOUT_STORAGE_KEY);
    rawHidden = localStorage.getItem(LEGACY_HIDDEN_STORAGE_KEY);
  } catch {
    return null;
  }
  const layoutOverride = parseLayout(rawLayout);
  const hidden = parseHidden(rawHidden);
  if (!layoutOverride && hidden.length === 0) return null;
  return { layoutOverride, hidden };
}

/** 迁移成功（或被 DB 内容取代）后清掉旧键；失败路径**绝不调用**。 */
export function clearLegacyState(): void {
  try {
    localStorage.removeItem(LEGACY_LAYOUT_STORAGE_KEY);
    localStorage.removeItem(LEGACY_HIDDEN_STORAGE_KEY);
  } catch {
    // 隐私模式等：留着，下次启动再清
  }
}

// ---- 网络 ---------------------------------------------------------------

/** GET /api/dashboard-layout → 会话状态；失败显式只读降级，不抛错。 */
export async function loadDashboardLayout(): Promise<LoadResult> {
  try {
    const payload = await getDashboardLayout();
    return { state: stateFromItems(payload.items), readOnly: false, error: null };
  } catch {
    return {
      state: { ...EMPTY_LAYOUT_STATE },
      readOnly: true,
      error: '布局同步服务不可用，总控台布局暂按默认只读显示',
    };
  }
}

/** PUT 整体替换。失败向上抛（调用方决定留在编辑态重试还是降级）。 */
export async function persistDashboardLayout(
  state: DashboardLayoutState,
  defaults: LayoutItem[]
): Promise<void> {
  await saveDashboardLayout(itemsFromState(state, defaults));
}
