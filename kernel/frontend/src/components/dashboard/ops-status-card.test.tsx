import { displayTime } from '@/lib/display-time';
import { describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  describeLastDeploy,
  describeTimedBackup,
  formatLastDeploy,
  formatOpsValue,
  formatTimedBackup,
  OpsStatusCard,
} from './ops-status-card';
import type { OpsStatus, OpsTimedBackup } from '@/lib/types';

vi.mock('@/lib/api', () => ({
  getOpsStatus: vi.fn(),
}));

// 七键均 null 的基线 fixture，各用例按需覆盖个别字段
const NULL_STATUS: OpsStatus = {
  running_commit: null,
  versions_last_checked: null,
  feed_last_fetched: null,
  last_backup_tag: null,
  last_deploy: null,
  last_probe_run: null,
  timed_backup: null,
};

const TIMED_BASE: OpsTimedBackup = {
  state: 'ok',
  last_attempt: '2026-09-08T04:10:12+08:00',
  last_success: '2026-09-08T04:10:12+08:00',
  snapshot: 'console.db.20260908',
  stage: null,
};

describe('formatOpsValue', () => {
  test('null / undefined / 空串 → 未知', () => {
    expect(formatOpsValue(null)).toBe('未知');
    expect(formatOpsValue(undefined)).toBe('未知');
    expect(formatOpsValue('')).toBe('未知');
    expect(formatOpsValue('   ')).toBe('未知');
  });

  test('有值原样返回', () => {
    expect(formatOpsValue('abc123')).toBe('abc123');
    expect(formatOpsValue('2026-08-03 12:00:00')).toBe('2026-08-03 12:00:00');
  });
});

describe('formatLastDeploy（A7 六键）', () => {
  test('null / undefined → 未知', () => {
    expect(formatLastDeploy(null)).toBe('未知');
    expect(formatLastDeploy(undefined)).toBe('未知');
  });

  test('commit 与 deployed_at 都在时拼接短哈希与部署时间', () => {
    expect(
      formatLastDeploy({
        commit: 'abcdef1234567',
        backup_tag: '20260906-214411',
        deployed_at: '2026-09-06T21:44:11+08:00',
        migrations_applied: 10,
      }),
    ).toBe(`abcdef1 · ${displayTime('2026-09-06T21:44:11+08:00').text}`);
  });

  test('只有 commit 时只显示短哈希', () => {
    expect(
      formatLastDeploy({
        commit: 'abcdef1234567',
        backup_tag: null,
        deployed_at: null,
        migrations_applied: null,
      }),
    ).toBe('abcdef1');
  });

  test('只有 deployed_at 时只显示部署时间', () => {
    expect(
      formatLastDeploy({
        commit: null,
        backup_tag: null,
        deployed_at: '2026-09-06T21:44:11+08:00',
        migrations_applied: null,
      }),
    ).toBe(displayTime('2026-09-06T21:44:11+08:00').text);
  });

  test('commit 与 deployed_at 都为 null 时即使对象/backup_tag 存在也显示未知，不伪装成有效值', () => {
    expect(
      formatLastDeploy({
        commit: null,
        backup_tag: 'x',
        deployed_at: null,
        migrations_applied: null,
      }),
    ).toBe('未知');
  });
});

describe('describeLastDeploy（A7 可访问文本）', () => {
  test('null → 未知', () => {
    expect(describeLastDeploy(null)).toBe('未知');
  });

  test('逐键列出，migrations_applied=0 显示 0 而不是未知', () => {
    const text = describeLastDeploy({
      commit: 'a',
      backup_tag: 'b',
      deployed_at: 'c',
      migrations_applied: 0,
    });
    expect(text).toContain('commit: a');
    expect(text).toContain('backup_tag: b');
    expect(text).toContain('deployed_at: c');
    expect(text).toContain('migrations_applied: 0');
  });

  test('缺失键各自显示未知', () => {
    const text = describeLastDeploy({
      commit: null,
      backup_tag: null,
      deployed_at: null,
      migrations_applied: null,
    });
    expect(text).toContain('commit: 未知');
    expect(text).toContain('backup_tag: 未知');
    expect(text).toContain('deployed_at: 未知');
    expect(text).toContain('migrations_applied: 未知');
  });
});

describe('formatTimedBackup（F17）', () => {
  test('null / undefined → 未知（旧后端无此键时不伪装成正常）', () => {
    expect(formatTimedBackup(null)).toBe('未知');
    expect(formatTimedBackup(undefined)).toBe('未知');
  });

  test('never → 从未', () => {
    expect(formatTimedBackup({ ...TIMED_BASE, state: 'never', last_success: null })).toBe('从未');
  });

  test('ok → 正常 + 成功时间（截到分钟）', () => {
    expect(formatTimedBackup(TIMED_BASE)).toBe(`正常 · ${displayTime('2026-09-08T04:10:12+08:00', { minute: true }).text}`);
  });

  test('stale → 陈旧 + 上次成功时间', () => {
    expect(
      formatTimedBackup({
        ...TIMED_BASE,
        state: 'stale',
        last_success: '2026-09-07T04:10:11+08:00',
      }),
    ).toBe(`陈旧 · ${displayTime('2026-09-07T04:10:11+08:00', { minute: true }).text}`);
  });

  test('failed → 失败(阶段) + 上次成功时间，阶段缺失时只写失败', () => {
    expect(
      formatTimedBackup({
        ...TIMED_BASE,
        state: 'failed',
        stage: 'quick_check',
        last_success: '2026-09-07T04:10:11+08:00',
      }),
    ).toBe(`失败(quick_check) · ${displayTime('2026-09-07T04:10:11+08:00', { minute: true }).text}`);
    expect(formatTimedBackup({ ...TIMED_BASE, state: 'failed', stage: null })).toBe(
      `失败 · ${displayTime('2026-09-08T04:10:12+08:00', { minute: true }).text}`,
    );
  });

  test('unknown → 未知，即使标记里有旧的成功时间', () => {
    expect(formatTimedBackup({ ...TIMED_BASE, state: 'unknown' })).toBe('未知');
  });

  test('有状态但没有 last_success 时只显示状态，不用当前时间顶替', () => {
    expect(formatTimedBackup({ ...TIMED_BASE, state: 'ok', last_success: null })).toBe('正常');
  });
});

describe('describeTimedBackup（F17 可访问文本）', () => {
  test('null → 未知', () => {
    expect(describeTimedBackup(null)).toBe('未知');
  });

  test('逐键列出，缺失键显示未知', () => {
    const text = describeTimedBackup({ ...TIMED_BASE, state: 'failed', stage: 'quick_check' });
    expect(text).toContain('state: failed');
    expect(text).toContain('stage: quick_check');
    expect(text).toContain('last_success: 2026-09-08T04:10:12+08:00');
    expect(text).toContain('snapshot: console.db.20260908');
    expect(describeTimedBackup({ ...TIMED_BASE, snapshot: null })).toContain('snapshot: 未知');
  });
});

describe('OpsStatusCard', () => {
  test('null 字段显示未知，有值显示原文（四个既有字段）', () => {
    const status: OpsStatus = {
      ...NULL_STATUS,
      versions_last_checked: '2026-08-03 12:30:00',
      last_backup_tag: '',
    };
    render(<OpsStatusCard status={status} />);
    expect(screen.getByText('运维状态')).toBeTruthy();
    expect(screen.getByText(displayTime('2026-08-03 12:30:00', { assumeUtc: true }).text)).toBeTruthy();
    // 六个「未知」：running_commit / feed_last_fetched / last_backup_tag / last_deploy /
    // last_probe_run / timed_backup
    expect(screen.getAllByText('未知')).toHaveLength(6);
  });

  test('七键均有值时正确显示，含 last_deploy 摘要、last_probe_run 与定时备份状态', () => {
    const status: OpsStatus = {
      running_commit: 'abcdef1234567',
      versions_last_checked: '2026-09-06 12:00:00',
      feed_last_fetched: '2026-09-06 12:05:00',
      last_backup_tag: '20260906-214411',
      last_deploy: {
        commit: 'abcdef1234567',
        backup_tag: '20260906-214411',
        deployed_at: '2026-09-06T21:44:11+08:00',
        migrations_applied: 10,
      },
      last_probe_run: '2026-09-07T02:30:00Z',
      timed_backup: TIMED_BASE,
    };
    render(<OpsStatusCard status={status} />);
    expect(screen.getByText(displayTime('2026-09-07T02:30:00Z').text)).toBeTruthy();
    expect(screen.getByText(`abcdef1 · ${displayTime('2026-09-06T21:44:11+08:00').text}`)).toBeTruthy();
    expect(screen.getByText(`正常 · ${displayTime('2026-09-08T04:10:12+08:00', { minute: true }).text}`)).toBeTruthy();
    expect(screen.queryAllByText('未知')).toHaveLength(0);
  });

  test('定时备份失败时卡片显示失败阶段，不显示为正常', () => {
    render(
      <OpsStatusCard
        status={{
          ...NULL_STATUS,
          timed_backup: { ...TIMED_BASE, state: 'failed', stage: 'quick_check' },
        }}
      />,
    );
    expect(screen.getByText(`失败(quick_check) · ${displayTime('2026-09-08T04:10:12+08:00', { minute: true }).text}`)).toBeTruthy();
    expect(screen.queryByText(`正常 · ${displayTime('2026-09-08T04:10:12+08:00', { minute: true }).text}`)).toBeNull();
  });

  test('last_deploy 对象存在但四键全为 null 时仍显示未知，不伪装成有效值', () => {
    render(
      <OpsStatusCard
        status={{
          ...NULL_STATUS,
          last_deploy: { commit: null, backup_tag: null, deployed_at: null, migrations_applied: null },
        }}
      />,
    );
    // 七个「未知」：其余六个字段本就 null，last_deploy 对象存在但四键皆空也归入未知
    expect(screen.getAllByText('未知')).toHaveLength(7);
  });

  test('migrations_applied=0 时可访问文本（title）显示 0 而不是未知', () => {
    const { container } = render(
      <OpsStatusCard
        status={{
          ...NULL_STATUS,
          last_deploy: { commit: null, backup_tag: null, deployed_at: null, migrations_applied: 0 },
        }}
      />,
    );
    const cell = container.querySelector('[title*="migrations_applied: 0"]');
    expect(cell).not.toBeNull();
    expect(cell?.textContent).toBe('未知');
  });

  test('七字段顺序固定：running_commit → versions_last_checked → feed_last_fetched → last_backup_tag → timed_backup → last_deploy → last_probe_run', () => {
    const { container } = render(<OpsStatusCard status={NULL_STATUS} />);
    const labels = Array.from(container.querySelectorAll('.text-xs.text-muted-foreground')).map(
      (el) => el.textContent,
    );
    expect(labels).toEqual([
      '运行 commit',
      '版本采集',
      'Feed 新鲜度',
      '最近备份',
      '定时备份',
      '最近部署',
      '探针巡检',
    ]);
  });
});
