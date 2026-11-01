import {
  ORPHAN_SYSTEM_PREFIX,
  machineWidgetId,
} from '@/components/dashboard/layout-model';
import type { WidgetKey } from '@/components/dashboard/layout-model';
import type { Machine, MonitorSystem } from '@/lib/types';

/**
 * C8 / D3 widget 显隐。
 *
 * **单独一个键、单独一个文件**，不动 D2 已上线的布局序列化契约：
 * 那份契约只认 `{key,x,y,w}`，`serializeLayout` 会把多余字段丢掉、`parseLayout` 会把
 * 带未知字段的项照收——把 `hidden` 塞进同一条记录既过不了它的校验语义，也会让
 * 「无 override = 与旧版像素级一致」这条 D2 验收失去一份干净的对照。
 *
 * 语义：隐藏集合是一串 widget key。空集合等价于「没设过」——
 * 总控台据此判断要不要走网格渲染路径。
 *
 * D1' 起存储收敛到 `lib/dashboard-layout-store.ts`（旧键
 * `console.dashboard.hidden` 由迁移路径读写后退役；DB 侧与坐标同表，
 * `hidden` 列承载）；本文件保留**纯模型逻辑**：解析 / 序列化 / 名称映射。
 */

const FIXED_LABELS: Record<string, string> = {
  // 'inspection' 已随 D4-2.3 收进顶栏；DB 里若还留着它的行，
  // widgetLabel 会退回 key 本身——不影响任何还存在的卡
  attention: '今日关注',
  'monitor-events': '监控事件',
  'ops-status': '运维状态',
  tailnet: 'Tailscale 网络',
  // C11 上线时漏登记，「添加 widget」列表里会显示技术 id `caddy-entry`；
  // 名字随 2026-09-07 口径复核统一成「入口上游」（卡片标题同步，widget key 不动）
  'caddy-entry': '入口上游',
};

export interface WidgetLabelSource {
  machines: Machine[];
  systems: MonitorSystem[];
}

/**
 * widget key → 给人看的名字。找不到台账/Beszel 记录就退回 key 本身，
 * 「添加 widget」列表宁可显示一个技术 id 也不能漏掉一张找不回来的卡。
 */
export function widgetLabel(key: WidgetKey, { machines, systems }: WidgetLabelSource): string {
  const fixed = FIXED_LABELS[key];
  if (fixed) return fixed;

  const id = machineWidgetId(key);
  if (!id) return key;

  if (id.startsWith(ORPHAN_SYSTEM_PREFIX)) {
    const sysId = id.slice(ORPHAN_SYSTEM_PREFIX.length);
    return systems.find((s) => s.id === sysId)?.name || sysId;
  }
  return machines.find((m) => m.id === id)?.label || id;
}

/** 坏数据一律降级成空集合，不让显隐偏好把总控台搞白屏（与 `parseLayout` 同纪律）。 */
export function parseHidden(raw: string | null | undefined): WidgetKey[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const seen = new Set<string>();
  for (const item of parsed) {
    if (typeof item === 'string' && item.length > 0) seen.add(item);
  }
  return Array.from(seen);
}

export function serializeHidden(keys: WidgetKey[]): string {
  return JSON.stringify(Array.from(new Set(keys)));
}
