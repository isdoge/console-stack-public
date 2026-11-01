import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { COMPARE_LIMIT, MachineCompare, buildCompareRows } from './machine-compare';
import type { CapacityPayload, Machine, MonitorSystem } from '@/lib/types';

const NAS: Machine = { id: 'nas', label: 'NAS', role: 'server', os: 'Debian 13' };
const KR: Machine = {
  id: 'kr',
  label: '光帆 KR',
  role: 'node',
  probe_status: 'up',
  traffic: {
    month: '2026-09',
    nic: 'ens5',
    sent_gb: 900,
    recv_gb: 120,
    used_gb: 900,
    quota_gb: 1000,
    pct: 90,
    since: '2026-09-01T00:00:00Z',
    projection: {
      projected_gb: 1350,
      daily_gb: 45,
      observed_hours: 480,
      remaining_days: 10,
      partial_month: false,
      quota_gb: 1000,
      projected_pct: 135,
      over_quota: true,
    },
  },
};
const SG: Machine = { id: 'sg', label: '光帆 SG', role: 'node', probe_status: 'down' };

const SYSTEMS: Record<string, MonitorSystem> = {
  nas: {
    id: '1',
    name: 'NAS',
    machine: 'nas',
    status: 'up',
    cpu: 12.5,
    memory_used_mb: 4096,
    memory_total_mb: 16384,
    disk_used_gb: 40.2,
    disk_total_gb: 412.8,
    uptime_sec: 864000,
  },
};

const CAPACITY: CapacityPayload = {
  ok: true,
  ttl: 3600,
  history_type: '480m',
  min_r_squared: 0.5,
  error: null,
  machines: {
    nas: {
      samples: 90,
      span_hours: 720,
      first_at: '2026-08-08T00:00:00Z',
      last_at: '2026-09-07T00:00:00Z',
      disk_total_gb: 412.8,
      disk_used_gb: 40.2,
      disk_free_gb: 372.6,
      daily_gb: 0.3,
      r_squared: 0.238,
      trend: 'rising',
      days_to_full: null,
    },
  },
};

function rowOf(rows: ReturnType<typeof buildCompareRows>, label: string) {
  const row = rows.find((r) => r.label === label);
  if (!row) throw new Error(`没有这一行：${label}`);
  return row;
}

describe('buildCompareRows（M3 纯函数）', () => {
  test('列顺序 = 传入顺序，每行值与机器一一对应', () => {
    const rows = buildCompareRows([KR, NAS], SYSTEMS, CAPACITY);
    expect(rowOf(rows, '角色').values).toEqual(['节点', '服务器']);
    // node 看 probe_status，server 看 Beszel status（与卡片同口径）
    expect(rowOf(rows, '状态').values).toEqual(['up', 'up']);
  });

  test('缺数据统一 — ，不是 0 也不是空字符串', () => {
    const rows = buildCompareRows([SG], {}, null);
    expect(rowOf(rows, 'CPU').values).toEqual(['—']);
    expect(rowOf(rows, '本月出站').values).toEqual(['—']);
    expect(rowOf(rows, '月底预计（M1）').values).toEqual(['—']);
    expect(rowOf(rows, '磁盘日增（M2）').values).toEqual(['—']);
    expect(rowOf(rows, '预计写满（M2）').values).toEqual(['—']);
    // 没有 renewal 记录时按长期算，与卡片一致
    expect(rowOf(rows, '到期').values).toEqual(['长期']);
    expect(rowOf(rows, '价格').values).toEqual(['—']);
  });

  test('价格复用卡片的 priceText：带币种符号与周期后缀，cost=0 显示免费', () => {
    const paid: Machine = {
      ...SG,
      renewal: { id: 'r1', cost: 5, currency: 'USD', cycle: 'monthly' },
    };
    const free: Machine = { ...NAS, renewal: { id: 'r2', cost: 0, currency: 'CNY' } };
    const rows = buildCompareRows([paid, free], SYSTEMS, CAPACITY);
    expect(rowOf(rows, '价格').values).toEqual(['$5 / 月', '免费']);
  });

  test('M1 预测超配额才上色；不超或没预测不上色', () => {
    const rows = buildCompareRows([KR, NAS], SYSTEMS, CAPACITY);
    const row = rowOf(rows, '月底预计（M1）');
    expect(row.values).toEqual(['1350 GB', '—']);
    expect(row.tones).toEqual(['text-amber-700', undefined]);
  });

  test('M2 有趋势但 days_to_full=null → 「暂不预测」且不上色（线上 6/6 台就是这一档）', () => {
    const rows = buildCompareRows([NAS], SYSTEMS, CAPACITY);
    expect(rowOf(rows, '磁盘日增（M2）').values).toEqual(['+0.3 GB/天']);
    const eta = rowOf(rows, '预计写满（M2）');
    expect(eta.values).toEqual(['暂不预测']);
    expect(eta.tones).toEqual([undefined]);
  });

  test('capacity 为 null（还没拉到）时 M2 两行退化成 — ，不抛', () => {
    const rows = buildCompareRows([NAS], SYSTEMS, null);
    expect(rowOf(rows, '磁盘日增（M2）').values).toEqual(['—']);
    expect(rowOf(rows, '预计写满（M2）').values).toEqual(['—']);
  });
});

describe('MachineCompare 对话框', () => {
  const machines = [NAS, KR, SG, { id: 'us', label: 'US', role: 'node' } as Machine,
    { id: 'txy', label: 'TXY', role: 'server' } as Machine];

  function open() {
    return render(
      <MachineCompare
        open
        machines={machines}
        systemByMachine={SYSTEMS}
        capacity={CAPACITY}
        onOpenChange={vi.fn()}
      />
    );
  }

  test('未选机器时给提示而不是空表（文案与实际行为一致：选一台就出表）', () => {
    open();
    expect(screen.getByText('先从上面选机器。')).toBeTruthy();
    expect(screen.queryByText('磁盘日增（M2）')).toBeNull();
  });

  test('选中后出现对比表，表头是机器名', () => {
    open();
    fireEvent.click(screen.getByRole('button', { name: 'NAS' }));
    expect(screen.getByText('磁盘日增（M2）')).toBeTruthy();
    expect(screen.getByText('+0.3 GB/天')).toBeTruthy();
  });

  test(`选满 ${COMPARE_LIMIT} 台后其余按钮禁用，已选的仍可取消`, () => {
    open();
    for (const label of ['NAS', '光帆 KR', '光帆 SG', 'US']) {
      fireEvent.click(screen.getByRole('button', { name: label }));
    }
    const overflow = screen.getByRole('button', { name: 'TXY' }) as HTMLButtonElement;
    expect(overflow.disabled).toBe(true);
    // 已选中的不受上限影响，点一下应能取消并把名额让出来
    fireEvent.click(screen.getByRole('button', { name: 'NAS' }));
    expect((screen.getByRole('button', { name: 'TXY' }) as HTMLButtonElement).disabled).toBe(false);
  });

  test('关闭再打开清空选择（临时视图，刻意不持久化）', () => {
    const { rerender } = open();
    fireEvent.click(screen.getByRole('button', { name: 'NAS' }));
    expect(screen.getByText('磁盘日增（M2）')).toBeTruthy();

    const props = {
      machines,
      systemByMachine: SYSTEMS,
      capacity: CAPACITY,
      onOpenChange: vi.fn(),
    };
    rerender(<MachineCompare open={false} {...props} />);
    rerender(<MachineCompare open {...props} />);
    expect(screen.getByText('先从上面选机器。')).toBeTruthy();
  });
});
