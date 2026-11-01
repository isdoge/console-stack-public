import { buildRoleMap, machineRole } from '@/components/monitor/partition';
import type { Machine, MonitorSystem } from '@/lib/types';

/**
 * C8 / D2 布局模型：`default ⊕ override`。
 *
 * - **default 现算**：按 machines 台账（+ Beszel 里有台账里没有的系统）每次渲染重新推导，
 *   所以机器增删不会留孤儿行、也不会缺行——这是评审要求用它替掉「与现状等价的 seed」的原因。
 * - **override 只存用户挪过的那部分**：读取时按 key 覆盖到 default 上，
 *   **未知 key 忽略、缺失 key 追加到网格末尾**。
 * - **只存 12 列坐标 `x/y/w`，不存 `h`**：高度由 GridStack 的 `resizeToContent` 按内容定，
 *   6 列 / 1 列两档由引擎的 `columnOpts.breakpoints` 现算，不落盘。
 *
 * D1' 起 override 的读写收敛到 `lib/dashboard-layout-store.ts`（SQLite v9，
 * localStorage 只剩一次性迁移路径）；本文件保留**纯模型逻辑**：
 * 默认布局现算、default ⊕ override 合并、序列化契约。
 */

/** 持久化基准列数。6 列 / 1 列不落盘。 */
export const GRID_COLUMNS = 12;

/** 机器卡 widget key 前缀，`machine-card:<台账 id>`。 */
export const MACHINE_WIDGET_PREFIX = 'machine-card:';

/**
 * 台账里没有、只在 Beszel 里出现的系统（含 `/api/machines` 未就绪时）用这个二级前缀，
 * 与台账机器区分开：`machine-card:sys:<beszel id>`。
 */
export const ORPHAN_SYSTEM_PREFIX = 'sys:';

/** 非机器卡的固定件，顺序与现有静态渲染树一致。
 * D4-2.3 起 `inspection` 收进顶栏（不再是正文区 widget），从默认布局移除；
 * DB 侧白名单仍保留该 key（dashboard_layout.py），D1'–D4 之间落库的行由整体替换自然清理。 */
export const FIXED_WIDGET_KEYS = [
  'attention',
  'monitor-events',
  'ops-status',
  'tailnet',
  'caddy-entry',
] as const;

export type FixedWidgetKey = (typeof FIXED_WIDGET_KEYS)[number];

/** `inspection` | `attention` | `monitor-events` | `ops-status` | `tailnet` | `machine-card:<id>` */
export type WidgetKey = string;

export interface LayoutItem {
  key: WidgetKey;
  x: number;
  y: number;
  w: number;
}

export interface DashboardWidgetSource {
  machines: Machine[];
  systems: MonitorSystem[];
}

/** 名义行距（cell 单位）。真实高度由 sizeToContent 决定，`float:false` 会把空隙压掉。 */
const ROW_STRIDE = 12;

export function machineWidgetKey(id: string): WidgetKey {
  return `${MACHINE_WIDGET_PREFIX}${id}`;
}

export function isMachineWidgetKey(key: WidgetKey): boolean {
  return key.startsWith(MACHINE_WIDGET_PREFIX);
}

/** 取出机器卡的标识；非机器卡返回 null。含 `sys:` 前缀时原样带回。 */
export function machineWidgetId(key: WidgetKey): string | null {
  return isMachineWidgetKey(key) ? key.slice(MACHINE_WIDGET_PREFIX.length) : null;
}

/** 默认宽度：机器卡 3（对齐现状 `xl:grid-cols-4`），其余整行 12。 */
export function defaultWidthOf(key: WidgetKey): number {
  return isMachineWidgetKey(key) ? 3 : GRID_COLUMNS;
}

/** 最小宽度：机器卡 ≥3、事件/横条卡 ≥4（C8 约束）。 */
export function minWidthOf(key: WidgetKey): number {
  return isMachineWidgetKey(key) ? 3 : 4;
}

/**
 * 默认 widget 顺序。与现有静态渲染树同序：
 * 今日关注 → 监控事件 → 服务器卡 → 节点卡 → 运维状态 → tailnet
 * （巡检报告动作 D4-2.3 起在顶栏，不占正文区）。
 *
 * 服务器卡以**台账**为准（Beszel 挂了默认布局也不塌），Beszel 里有、台账里没有的系统
 * 追加在服务器卡之后——静态渲染树对这种系统是照渲染的（`/api/machines` 未就绪即属此类），
 * 网格路径不能把它丢了。
 */
export function defaultWidgetKeys({ machines, systems }: DashboardWidgetSource): WidgetKey[] {
  const roleMap = buildRoleMap(machines);
  const known = new Set(machines.map((m) => m.id));
  const keys: WidgetKey[] = ['attention', 'monitor-events'];

  for (const m of machines) if (m.role !== 'node') keys.push(machineWidgetKey(m.id));
  for (const sys of systems) {
    if (sys.machine && known.has(sys.machine)) continue;
    if (machineRole(sys.machine, roleMap) === 'node') continue;
    keys.push(machineWidgetKey(`${ORPHAN_SYSTEM_PREFIX}${sys.id}`));
  }
  for (const m of machines) if (m.role === 'node') keys.push(machineWidgetKey(m.id));

  keys.push('ops-status', 'tailnet', 'caddy-entry');
  return keys;
}

/** 按顺序铺格：整行件独占一行，机器卡一行四张。 */
function pack(keys: WidgetKey[], startY = 0): LayoutItem[] {
  const items: LayoutItem[] = [];
  let x = 0;
  let y = startY;
  for (const key of keys) {
    const w = defaultWidthOf(key);
    if (x + w > GRID_COLUMNS) {
      x = 0;
      y += ROW_STRIDE;
    }
    items.push({ key, x, y, w });
    x += w;
    if (x >= GRID_COLUMNS) {
      x = 0;
      y += ROW_STRIDE;
    }
  }
  return items;
}

export function buildDefaultLayout(source: DashboardWidgetSource): LayoutItem[] {
  return pack(defaultWidgetKeys(source));
}

/**
 * `default ⊕ override`：
 * - override 里 default 没有的 key **忽略**（机器下架后留下的孤儿行）；
 * - default 里 override 没有的 key **追加到网格末尾**（新机器自动上场）；
 * - 两边都有的，坐标以 override 为准。
 */
export function mergeLayout(defaults: LayoutItem[], override: LayoutItem[] | null): LayoutItem[] {
  if (!override || override.length === 0) return defaults;

  const wanted = new Set(defaults.map((d) => d.key));
  const byKey = new Map<WidgetKey, LayoutItem>();
  for (const item of override) {
    if (!wanted.has(item.key) || byKey.has(item.key)) continue; // 未知 key / 重复 key 忽略
    byKey.set(item.key, item);
  }
  if (byKey.size === 0) return defaults;

  const kept = defaults
    .filter((d) => byKey.has(d.key))
    .map((d) => {
      const o = byKey.get(d.key)!;
      // 宽度仍受 min w 约束：override 是用户数据，不能靠它守约束
      return { key: d.key, x: o.x, y: o.y, w: Math.max(o.w, minWidthOf(d.key)) };
    });

  const missing = defaults.filter((d) => !byKey.has(d.key)).map((d) => d.key);
  if (missing.length === 0) return kept;

  const nextY = kept.reduce((max, it) => Math.max(max, it.y), 0) + ROW_STRIDE;
  return [...kept, ...pack(missing, nextY)];
}

/** 只序列化 `key/x/y/w`——`h` 不进存储，多余字段一律丢弃。 */
export function serializeLayout(items: LayoutItem[]): string {
  return JSON.stringify(items.map(({ key, x, y, w }) => ({ key, x, y, w })));
}

function isValidItem(raw: unknown): raw is LayoutItem {
  if (!raw || typeof raw !== 'object') return false;
  const { key, x, y, w } = raw as Record<string, unknown>;
  if (typeof key !== 'string' || key.length === 0) return false;
  for (const n of [x, y, w]) {
    if (typeof n !== 'number' || !Number.isInteger(n) || n < 0) return false;
  }
  const nx = x as number;
  const nw = w as number;
  return nw >= 1 && nx + nw <= GRID_COLUMNS;
}

/**
 * 解析 override。坏数据一律降级成「没有 override」而不是抛错——
 * 布局偏好不值得让总控台白屏。整条 JSON 坏 → null；单项坏 → 丢那一项。
 */
export function parseLayout(raw: string | null | undefined): LayoutItem[] | null {
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) return null;
  const items = parsed.filter(isValidItem).map(({ key, x, y, w }) => ({ key, x, y, w }));
  return items.length > 0 ? items : null;
}
