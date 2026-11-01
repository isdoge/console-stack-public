import { describe, expect, it } from 'vitest';
import {
  projectBillingToMonth,
  sumChargesByCurrency,
  type BillingProjectionRow,
} from './billing-projection';

function row(overrides: Partial<BillingProjectionRow>): BillingProjectionRow {
  return {
    id: overrides.id ?? 'r1',
    name: overrides.name ?? '条目',
    kind: overrides.kind ?? 'subscription',
    expires_at: overrides.expires_at ?? null,
    starts_at: overrides.starts_at ?? null,
    created_at: overrides.created_at ?? '2026-01-01',
    cost: overrides.cost ?? 10,
    currency: overrides.currency ?? 'CNY',
    cycle: overrides.cycle ?? 'yearly',
  };
}

describe('billing-projection', () => {
  it('only active subscriptions produce projected charges', () => {
    const active = row({ cycle: 'monthly', starts_at: '2026-01-01' });
    expect(projectBillingToMonth([
      active, { ...active, id: 'ended', status: 'ended' },
      { ...active, id: 'cancel', status: 'not_renewing' },
    ], 2026, 9).map((charge) => charge.renewalId)).toEqual([active.id]);
  });
  it.each([0, 13, 1.5, NaN])('rejects invalid target month %s', (month) => {
    expect(projectBillingToMonth([row({ cycle: 'monthly' })], 2026, month)).toEqual([]);
  });

  it.each([0, 10000, 2026.5, NaN])('rejects invalid target year %s', (year) => {
    expect(projectBillingToMonth([row({ cycle: 'monthly' })], year, 1)).toEqual([]);
  });

  it('does not normalize impossible anchors and ignores non-finite costs', () => {
    const invalid = row({ starts_at: '2026-02-30', expires_at: 'bad', created_at: 'bad', cycle: 'monthly' });
    expect(projectBillingToMonth([invalid], 2026, 3)).toEqual([]);
    const costs = [NaN, Infinity].map((cost) => row({ cost, cycle: 'monthly' }));
    expect(projectBillingToMonth(costs, 2026, 3)).toEqual([]);
  });

  it('sums currencies that collide with object prototype names as normal keys', () => {
    const charges = projectBillingToMonth([
      row({ currency: '__proto__', cycle: 'monthly', cost: 10 }),
      row({ currency: 'constructor', cycle: 'monthly', cost: 20 }),
    ], 2026, 3);
    const totals = sumChargesByCurrency(charges);
    expect(totals['__proto__']).toBe(10);
    expect(totals.constructor).toBe(20);
  });

  it('月付从锚点月起每月都记一笔', () => {
    const r = row({ id: 'm', name: '月付', starts_at: '2026-01-20', cycle: 'monthly', cost: 20 });
    expect(projectBillingToMonth([r], 2025, 12)).toEqual([]);
    expect(projectBillingToMonth([r], 2026, 1)).toEqual([
      { date: '2026-01-20', renewalId: 'm', name: '月付', kind: 'subscription', cost: 20, currency: 'CNY', cycle: 'monthly' },
    ]);
    expect(projectBillingToMonth([r], 2026, 8).map((c) => c.date)).toEqual(['2026-08-20']);
    expect(projectBillingToMonth([r], 2027, 2).map((c) => c.date)).toEqual(['2027-02-20']);
  });

  it('年付在周年月记全额，非周年月不记', () => {
    const r = row({ id: 'y', name: '域名', starts_at: '2025-09-10', cycle: 'yearly', cost: 66 });
    expect(projectBillingToMonth([r], 2026, 8)).toEqual([]);
    expect(projectBillingToMonth([r], 2026, 9).map((c) => c.date)).toEqual(['2026-09-10']);
    expect(projectBillingToMonth([r], 2027, 9)[0].cost).toBe(66);
  });

  it('多年付按周期间隔记全额不摊', () => {
    const biennial = row({ id: 'b', starts_at: '2024-03-01', cycle: 'biennial', cost: 400 });
    expect(projectBillingToMonth([biennial], 2026, 3).map((c) => c.date)).toEqual(['2026-03-01']);
    expect(projectBillingToMonth([biennial], 2026, 4)).toEqual([]);
    expect(projectBillingToMonth([biennial], 2027, 3)).toEqual([]);
    expect(projectBillingToMonth([biennial], 2028, 3)[0].cost).toBe(400);

    const triennial = row({ id: 't', starts_at: '2024-01-15', cycle: 'triennial', cost: 900 });
    expect(projectBillingToMonth([triennial], 2026, 1)).toEqual([]);
    expect(projectBillingToMonth([triennial], 2027, 1).map((c) => c.date)).toEqual(['2027-01-15']);
    expect(projectBillingToMonth([triennial], 2027, 1)[0].cost).toBe(900);
  });

  it('季度付每 3 个月记一次', () => {
    const r = row({ id: 'q', starts_at: '2026-06-01', cycle: 'quarterly', cost: 30 });
    expect(projectBillingToMonth([r], 2026, 7)).toEqual([]);
    expect(projectBillingToMonth([r], 2026, 9).map((c) => c.date)).toEqual(['2026-09-01']);
    expect(projectBillingToMonth([r], 2026, 12).map((c) => c.date)).toEqual(['2026-12-01']);
  });

  it('一次性买断只在首次投入月记一笔', () => {
    const r = row({ id: 'o', starts_at: '2026-03-03', expires_at: '2029-03-03', cycle: 'onetime', cost: 415.8 });
    expect(projectBillingToMonth([r], 2026, 3).map((c) => c.date)).toEqual(['2026-03-03']);
    expect(projectBillingToMonth([r], 2026, 4)).toEqual([]);
    expect(projectBillingToMonth([r], 2027, 3)).toEqual([]);
  });

  it('一次性买断缺 starts_at 时回退 created_at 而非 expires_at', () => {
    const r = row({
      id: 'o2',
      starts_at: null,
      expires_at: '2029-03-03',
      created_at: '2026-03-05',
      cycle: 'onetime',
      cost: 100,
    });
    expect(projectBillingToMonth([r], 2026, 3).map((c) => c.date)).toEqual(['2026-03-05']);
    expect(projectBillingToMonth([r], 2029, 3)).toEqual([]);
  });

  it('叠加充值与无周期条目不投影', () => {
    const topup = row({ id: 'p', cycle: 'topup', expires_at: null, cost: 14 });
    const noCycle = row({ id: 'n', cycle: null });
    const noCost = row({ id: 'c', cycle: 'yearly', starts_at: '2026-01-01', cost: 0 });
    expect(projectBillingToMonth([topup, noCycle, noCost], 2026, 8)).toEqual([]);
  });

  it('周期条目缺 starts_at 时回退 expires_at 作为扣费锚点', () => {
    const r = row({
      id: 'e',
      starts_at: null,
      expires_at: '2026-12-01',
      created_at: '2026-02-01',
      cycle: 'yearly',
      cost: 88,
    });
    expect(projectBillingToMonth([r], 2026, 12).map((c) => c.date)).toEqual(['2026-12-01']);
    // created_at 是 2 月，若错误地回退 created_at 会在 2027-02 记一笔。
    expect(projectBillingToMonth([r], 2027, 2)).toEqual([]);
  });

  it('锚点完全缺失时不投影', () => {
    const r = row({
      id: 'x',
      starts_at: null,
      expires_at: null,
      created_at: null,
      cycle: 'yearly',
    });
    expect(projectBillingToMonth([r], 2026, 8)).toEqual([]);
  });

  it('锚点日超出目标月天数时 clamp 到月末', () => {
    const r = row({ id: 'c31', starts_at: '2026-01-31', cycle: 'monthly', cost: 5 });
    expect(projectBillingToMonth([r], 2027, 2).map((c) => c.date)).toEqual(['2027-02-28']);
    expect(projectBillingToMonth([r], 2026, 1).map((c) => c.date)).toEqual(['2026-01-31']);
  });

  it('同月多个扣费点按日期排序，同日按名称排序', () => {
    const a = row({ id: 'a', name: '甲', starts_at: '2026-09-15', cycle: 'yearly' });
    const b = row({ id: 'b', name: '乙', starts_at: '2026-09-01', cycle: 'yearly' });
    const c = row({ id: 'c', name: '丙', starts_at: '2026-09-15', cycle: 'yearly' });
    expect(projectBillingToMonth([c, a, b], 2026, 9).map((x) => x.name)).toEqual(['乙', '丙', '甲']);
  });

  it('sumChargesByCurrency 按币种分组合计不换算', () => {
    const rows = [
      row({ id: 'u1', cycle: 'monthly', starts_at: '2026-01-01', cost: 20, currency: 'USD' }),
      row({ id: 'u2', cycle: 'monthly', starts_at: '2026-01-01', cost: 5, currency: 'USD' }),
      row({ id: 'c1', cycle: 'yearly', starts_at: '2025-08-01', cost: 66, currency: 'CNY' }),
      row({ id: 'c2', cycle: 'yearly', starts_at: '2025-08-10', cost: 10, currency: 'CNY' }),
    ];
    const charges = projectBillingToMonth(rows, 2026, 8);
    expect(charges).toHaveLength(4);
    expect(sumChargesByCurrency(charges)).toEqual({ USD: 25, CNY: 76 });
  });

  it('未标币种的条目归入 CNY 组（与页面 currencyHint 惯例一致）', () => {
    const r = row({ id: 'nc', starts_at: '2026-08-01', cycle: 'yearly', cost: 9, currency: null });
    expect(sumChargesByCurrency(projectBillingToMonth([r], 2026, 8))).toEqual({ CNY: 9 });
  });

  it('支持 yearly 历史别名 year / annual', () => {
    const alias = row({ id: 'y2', starts_at: '2025-10-01', cycle: 'annual', cost: 12 });
    expect(projectBillingToMonth([alias], 2026, 10)).toHaveLength(1);
    expect(projectBillingToMonth([alias], 2026, 11)).toEqual([]);
  });
});
