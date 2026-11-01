import { displayTime } from '@/lib/display-time';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { UnifiedTable, certDisplay, identityKey, isOutdated, mergeRows } from './unified-table';
import type { MonitorContainer, Service } from '@/lib/types';

describe('identityKey / mergeRows', () => {
  it('uses machine + container so three beszel-agent rows do not collapse', () => {
    const services: Service[] = [
      { name: 'beszel-agent', machine: 'nas', container: 'beszel-agent', actions: [] },
      { name: 'beszel-agent-txy', machine: 'txy', container: 'beszel-agent', actions: [] },
      { name: 'beszel-agent-aly', machine: 'aly', container: 'beszel-agent', actions: [] },
    ];
    const containers: MonitorContainer[] = [
      { system_id: '1', system_name: 'NAS', machine: 'nas', name: 'beszel-agent', status: 'running' },
      { system_id: '2', system_name: 'TXY', machine: 'txy', name: 'beszel-agent', status: 'running' },
      { system_id: '3', system_name: 'ALY', machine: 'aly', name: 'beszel-agent', status: 'running' },
    ];

    const rows = mergeRows(services, containers);

    expect(rows).toHaveLength(3);
    expect(rows.map((r) => r.name).sort()).toEqual([
      'beszel-agent',
      'beszel-agent-aly',
      'beszel-agent-txy',
    ]);
    expect(rows.every((r) => r.service && isStatusLike(r.runStatus))).toBe(true);
    expect(rows.every((r) => r.machine === r.service?.machine)).toBe(true);
  });

  it('falls back to service name when container field is absent', () => {
    const services: Service[] = [{ name: 'console-brain', machine: 'nas' }];
    const containers: MonitorContainer[] = [
      { system_id: '1', system_name: 'NAS', machine: 'nas', name: 'console-brain', status: 'up' },
    ];
    const rows = mergeRows(services, containers);
    expect(rows).toHaveLength(1);
    expect(rows[0].key).toBe('svc-nas-console-brain');
    expect(rows[0].runStatus).toBe('up');
  });

  it('keeps unmatched containers as orphan rows', () => {
    const rows = mergeRows(
      [{ name: 'known', machine: 'nas', container: 'known' }],
      [
        { system_id: '1', machine: 'nas', name: 'known', status: 'running' },
        { system_id: '1', machine: 'nas', name: 'orphan', status: 'running' },
      ],
    );
    expect(rows.map((r) => r.name)).toEqual(['known', 'orphan']);
    expect(rows[1].service).toBeUndefined();
  });

  it('identityKey normalizes separators and machine case', () => {
    expect(identityKey('NAS', 'beszel-agent')).toBe(identityKey('nas', 'beszel_agent'));
  });

  it('does not mark local-only or unknown versions as outdated', () => {
    expect(isOutdated({
      name: 'mysql',
      machine: 'txy',
      version_info: { local: '8.4.8', upstream: '未知' },
    })).toBe(false);
    expect(isOutdated({
      name: 'unknown-local',
      machine: 'nas',
      version_info: { local: 'N/A', upstream: 'v1.2.3' },
    })).toBe(false);
    expect(isOutdated({
      name: 'real-update',
      machine: 'nas',
      version_info: { local: '1.2.2', upstream: 'v1.2.3' },
    })).toBe(true);
  });
});

function isStatusLike(status?: string): boolean {
  return !!(status && status.length > 0);
}

describe('certDisplay（T3 §4.4 / D6）', () => {
  it('只接受 ssl_ok=true 且 ssl_days 是有限数字，色档 >30 / 8–30 / <=7', () => {
    expect(certDisplay({ status: 'Healthy', ssl_ok: true, ssl_days: 42 })).toEqual({
      text: '42 天',
      tone: 'ok',
    });
    expect(certDisplay({ status: 'Healthy', ssl_ok: true, ssl_days: 30 })).toEqual({
      text: '30 天',
      tone: 'warn',
    });
    expect(certDisplay({ status: 'Healthy', ssl_ok: true, ssl_days: 8 })).toEqual({
      text: '8 天',
      tone: 'warn',
    });
    expect(certDisplay({ status: 'Healthy', ssl_ok: true, ssl_days: 7 })).toEqual({
      text: '7 天',
      tone: 'bad',
    });
  });

  it('失败/未探测/非法值一律 null，错误文案绝不转数字', () => {
    expect(certDisplay({ status: 'Unhealthy', ssl_ok: false })).toBeNull();
    expect(certDisplay({ status: 'Healthy', msg: 'tls: handshake timeout' })).toBeNull();
    expect(certDisplay({ status: 'Healthy', ssl_ok: true })).toBeNull();
    expect(certDisplay({ status: 'Healthy', ssl_ok: true, ssl_days: Number.NaN })).toBeNull();
    expect(certDisplay(undefined)).toBeNull();
  });
});

describe('UnifiedTable 证书列（D6）', () => {
  function renderTable(services: Service[]) {
    return render(
      <UnifiedTable
        services={services}
        containers={[]}
        loading={false}
        onAdd={() => {}}
        onSettings={() => {}}
        onAction={() => {}}
        onVersion={() => {}}
        onBatchPriority={async () => {}}
      />
    );
  }

  it('默认可见「证书」列并渲染可信值', () => {
    renderTable([
      { name: 'svc-ok', machine: 'nas', health: { status: 'Healthy', ssl_ok: true, ssl_days: 42 } },
    ]);
    expect(screen.getByText('证书')).toBeTruthy();
    expect(screen.getByText('42 天')).toBeTruthy();
  });

  it('失败/无值显示 —，不出现 NaN 或错误文案', () => {
    renderTable([
      { name: 'svc-bad', machine: 'nas', health: { status: 'Unhealthy', msg: 'HTTP 500' } },
      { name: 'svc-none', machine: 'nas' },
    ]);
    expect(screen.queryByText('HTTP 500')).toBeNull();
    expect(screen.queryByText('NaN 天')).toBeNull();
    expect(screen.queryByText('42 天')).toBeNull();
    expect(screen.getAllByText('-').length).toBeGreaterThanOrEqual(2);
  });
});

describe('UnifiedTable 入口列（C5 → L4 入口维度）', () => {
  function renderTable(services: Service[]) {
    return render(
      <UnifiedTable
        services={services}
        containers={[]}
        loading={false}
        onAdd={() => {}}
        onSettings={() => {}}
        onAction={() => {}}
        onVersion={() => {}}
        onBatchPriority={async () => {}}
      />
    );
  }

  it('默认可见「入口」列，按 域名 › 边缘 › tailnet 渲染链路', () => {
    renderTable([
      {
        name: 'sublinkpro',
        machine: 'nas',
        entry: {
          domain: 'https://sub.example.test',
          edge: 'caddy',
          edge_known: true,
          tailscale: true,
          home: false,
          layers: ['domain', 'edge', 'tailscale'],
        },
      },
    ]);
    expect(screen.getByText('入口')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'sub.example.test' })).toBeTruthy();
    expect(screen.getByText('caddy')).toBeTruthy();
    expect(screen.getByText('tailnet')).toBeTruthy();
  });

  it('后端没给 entry（旧版本 / 降级）时该单元格是短横线，不报错', () => {
    renderTable([{ name: 'svc-old', machine: 'nas' }]);
    expect(screen.getByText('入口')).toBeTruthy();
    expect(screen.getAllByText('-').length).toBeGreaterThanOrEqual(1);
  });
});

describe('HealthMiniBand（T12 分支：服务健康迷你色带）', () => {
  const renderTable = (services: Service[]) =>
    render(
      <UnifiedTable
        services={services}
        containers={[]}
        loading={false}
        onAdd={() => {}}
        onSettings={() => {}}
        onAction={() => {}}
        onVersion={() => {}}
        onBatchPriority={async () => {}}
      />
    );

  it('有 health_history 时状态格渲染迷你色带，无样本不渲染', () => {
    // Tooltip uses the viewer's local time; keep the fixture stable in UTC CI too.
    const sampleTime = (minute: number) => new Date(2026, 8, 3, 8, minute).toISOString();
    const { container } = renderTable([
      {
        name: 'svc-a',
        machine: 'nas',
        health: { status: 'Healthy' },
        health_history: [
          { ts: sampleTime(0), status: 'Healthy' },
          { ts: sampleTime(10), status: 'Unhealthy' },
          { ts: sampleTime(20), status: 'Healthy' },
        ],
      },
      { name: 'svc-b', machine: 'nas', health: { status: 'Unknown' } },
    ]);
    expect(screen.getByRole('img', { name: '最近 3 次健康检查' })).toBeTruthy();
    expect(screen.getByTitle(`${displayTime(sampleTime(0)).title} · 健康`, { normalizer: (value) => value })).toBeTruthy();
    // 故障格 1 个（父容器 title 也含「故障」二字，用结尾锚定只匹配单元格）
    expect(screen.getAllByTitle(/· 故障$/).length).toBe(1);
    // svc-b 无历史：只有一个 role=img（svc-a 的）
    expect(container.querySelectorAll('[role="img"][aria-label*="健康检查"]').length).toBe(1);
  });
});

describe('单机视图的服务器过滤跟随（用户实测 bug 回归）', () => {
  it('hiddenMachines 含本机时，单机视图仍显示本机服务行', () => {
    // 用户在全部视图隐藏过 txy → 切到 txy 单机页时服务曾被自己的过滤器藏空
    localStorage.setItem('console.dashboard.hidden-machines', JSON.stringify(['txy']));
    const { container } = render(
      <UnifiedTable
        services={[{ name: 'axonhub', machine: 'txy', health: { status: 'Healthy' } }]}
        containers={[]}
        loading={false}
        activeMachine="txy"
        onAdd={() => {}}
        onSettings={() => {}}
        onAction={() => {}}
        onVersion={() => {}}
        onBatchPriority={async () => {}}
      />
    );
    expect(screen.getByText('axonhub')).toBeTruthy();
    expect(screen.queryByText('暂无服务数据')).toBeNull();
    void container;
    localStorage.removeItem('console.dashboard.hidden-machines');
  });
});

describe('隐私模式 / 存储异常降级（A3 safe-browser-storage）', () => {
  function renderTable() {
    return render(
      <UnifiedTable
        services={[{ name: 'svc-a', machine: 'nas' }]}
        containers={[]}
        loading={false}
        onAdd={() => {}}
        onSettings={() => {}}
        onAction={() => {}}
        onVersion={() => {}}
        onBatchPriority={async () => {}}
      />
    );
  }

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('localStorage.getItem 抛异常时表格仍能渲染（loadPageSize 兜底默认每页 10）', () => {
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    renderTable();
    expect(screen.getByText('svc-a')).toBeTruthy();
    expect(screen.getByText(/第 1 \/ 1 页/)).toBeTruthy();
  });

  it('localStorage.setItem 抛异常时仍可切换每页条数并完成一次交互', () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    renderTable();
    fireEvent.click(screen.getByRole('button', { name: '20' }));
    expect(screen.getByText('svc-a')).toBeTruthy();
  });
});
