/** 月度成本日历：按 cycle 把续费条目投影到指定月份的扣费点（纯函数，供 vitest 覆盖）。 */

import type { RenewalStatus } from './types';
import { calendarDate, parseCalendarDate, parseCreatedAt } from './calendar-date';
import { isActiveRenewal } from './renewal-status';

export interface BillingCharge<K extends string = string> {
  /** 扣费点日期，YYYY-MM-DD。 */
  date: string;
  renewalId: string;
  name: string;
  kind: K;
  /** 扣费金额：周期性条目每次扣费都记全额，多年付不摊。 */
  cost: number;
  /** 归组币种；条目未标币种沿用页面惯例默认 CNY。 */
  currency: string;
  cycle: string;
}

export interface BillingProjectionRow<K extends string = string> {
  id: string;
  name: string;
  kind: K;
  starts_at?: string | null;
  expires_at: string | null;
  created_at?: string;
  cost?: number | null;
  currency?: string | null;
  cycle?: string | null;
  status?: RenewalStatus;
}

/** 定期续费周期 → 相邻两次扣费间隔的月数（含 yearly 历史别名）。 */
const MONTH_INTERVALS: Record<string, number> = {
  monthly: 1,
  quarterly: 3,
  yearly: 12,
  year: 12,
  annual: 12,
  biennial: 24,
  triennial: 36,
};

/** 一次性买断：只在首次投入月记一笔。 */
const ONETIME_CYCLE = 'onetime';

/** 叠加充值：无时间维度，不产生扣费点。 */
const TOPUP_CYCLE = 'topup';

/**
 * 投影锚点：
 * - 周期性条目优先 starts_at，缺失回退 expires_at（到期日就是下一次真实扣费点），再回退 created_at；
 * - 一次性买断沿用 v5 口径 starts_at ?? created_at（expires_at 是覆盖终点，不是投入月）。
 */
function anchorDate(
  row: Pick<BillingProjectionRow, 'starts_at' | 'expires_at' | 'created_at' | 'cycle'>,
): Date | null {
  const start = parseCalendarDate(row.starts_at);
  if (start) return start;
  if (row.cycle?.toLowerCase() === ONETIME_CYCLE) return parseCreatedAt(row.created_at);
  return parseCalendarDate(row.expires_at) ?? parseCreatedAt(row.created_at);
}

/** 锚点日 clamp 到目标月最后一天（如 31 日锚点投影到 2 月 → 2 月末）。 */
function clampDay(anchor: Date, year: number, month: number): number {
  const last = new Date(0);
  last.setUTCFullYear(year, month, 0);
  const lastDay = last.getUTCDate();
  return Math.min(anchor.getUTCDate(), lastDay);
}

/**
 * 把续费条目投影到 (year, month) 的扣费点，按日期、名称排序。
 *
 * v5 cycle 语义：年付/多年付在周年月记全额（不摊）、月付每月记、季度每 3 个月记、
 * 一次性只在首次投入月记一笔、叠加充值不投影；锚点之前的月份不投影。
 * 币种只归组不换算，由 sumChargesByCurrency 分组展示。
 */
export function projectBillingToMonth<K extends string = string>(
  rows: BillingProjectionRow<K>[],
  year: number,
  month: number,
): BillingCharge<K>[] {
  const charges: BillingCharge<K>[] = [];
  if (!calendarDate(year, month, 1)) return charges;
  for (const row of rows) {
    if (!isActiveRenewal(row)) continue;
    if (row.cost == null || !Number.isFinite(row.cost) || row.cost <= 0) continue;
    const cycle = row.cycle?.toLowerCase();
    if (!cycle || cycle === TOPUP_CYCLE) continue;

    const anchor = anchorDate(row);
    if (!anchor) continue;

    const monthDiff = (year - anchor.getUTCFullYear()) * 12 + (month - 1 - anchor.getUTCMonth());
    if (monthDiff < 0) continue;
    if (cycle === ONETIME_CYCLE) {
      if (monthDiff !== 0) continue;
    } else {
      const interval = MONTH_INTERVALS[cycle];
      if (!interval || monthDiff % interval !== 0) continue;
    }

    const day = clampDay(anchor, year, month);
    charges.push({
      date: `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`,
      renewalId: row.id,
      name: row.name,
      kind: row.kind,
      cost: row.cost,
      currency: row.currency || 'CNY',
      cycle: row.cycle as string,
    });
  }
  return charges.sort(
    (a, b) => a.date.localeCompare(b.date) || a.name.localeCompare(b.name) || a.renewalId.localeCompare(b.renewalId),
  );
}

/** 按币种合计扣费金额；只归组不做汇率换算（币种混算会虚标成本）。 */
export function sumChargesByCurrency(charges: BillingCharge[]): Record<string, number> {
  const totals: Record<string, number> = Object.create(null);
  for (const charge of charges) {
    if (!Number.isFinite(charge.cost)) continue;
    totals[charge.currency] = (totals[charge.currency] ?? 0) + charge.cost;
  }
  return totals;
}
