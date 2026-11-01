import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { runMachineAction } from '@/lib/api';
import {
  MachineCard,
  formatMbPair,
  formatRate,
  meterTone,
  moneyText,
  priceText,
  projectionText,
  publicProbeText,
  remainingValue,
  statusTone,
  uptimeText,
} from './machine-card';
import type { Machine, MachineTraffic, MonitorSystem } from '@/lib/types';

vi.mock('@/lib/api', () => ({ runMachineAction: vi.fn() }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const KR: Machine = {
  id: 'kr',
  label: '光帆 KR',
  role: 'node',
  probe_status: 'up',
  public_host: 'kr.sunxc.eu.cc',
  probe: { type: 'tcp', port: 62037 },
};

describe('priceText（§4.3 价格 chip）', () => {
  test('cost 缺失 → null：不知道价格不等于免费，chip 整个不渲染', () => {
    expect(priceText({ id: 'x' })).toBeNull();
    expect(priceText(null)).toBeNull();
    expect(priceText(undefined)).toBeNull();
  });

  test('cost=0 → 免费（owner 已确认 kr/sg/us 免费，§3.2）', () => {
    expect(priceText({ id: 'x', cost: 0, currency: 'CNY', cycle: 'yearly' })).toBe('免费');
  });

  test('有价有周期 → ¥79 / 年（§4.2 txy）', () => {
    expect(priceText({ id: 'x', cost: 79, currency: 'CNY', cycle: 'yearly' })).toBe('¥79 / 年');
  });

  test('USD 换 $ 符号', () => {
    expect(priceText({ id: 'x', cost: 5, currency: 'USD', cycle: 'monthly' })).toBe('$5 / 月');
  });

  test('无周期时只显金额，不硬编一个「年」', () => {
    expect(priceText({ id: 'x', cost: 100, currency: 'CNY' })).toBe('¥100');
  });

  test('小数价格原样显示，不四舍五入（§3.3 校验数据就是 69.99）', () => {
    expect(priceText({ id: 'x', cost: 69.99, currency: 'CNY', cycle: 'yearly' })).toBe(
      '¥69.99 / 年'
    );
  });

  test('认不出的币种回落成「金额 + 代码」，不硬当人民币', () => {
    expect(priceText({ id: 'x', cost: 12, currency: 'EUR', cycle: 'monthly' })).toBe('12 EUR / 月');
    expect(priceText({ id: 'x', cost: 12, cycle: 'monthly' })).toBe('12 / 月');
  });
});

describe('moneyText（价格 chip 与剩余价值共用一套币种映射）', () => {
  test('小数位归调用方，helper 不擅自舍入', () => {
    expect(moneyText('69.99', 'CNY')).toBe('¥69.99');
    expect(moneyText('0.50', 'USD')).toBe('$0.50');
  });

  test('未知币种与缺币种', () => {
    expect(moneyText('12', 'EUR')).toBe('12 EUR');
    expect(moneyText('12')).toBe('12');
  });
});

describe('remainingValue（§3.3 日单价 × 剩余天数）', () => {
  test('参考图校验：¥69.99/年 + 剩余 180 天 → 34.5（图上 ¥34.52）', () => {
    const v = remainingValue({ id: 'x', cost: 69.99, cycle: 'yearly', days_left: 180 });
    expect(v).not.toBeNull();
    expect(v!).toBeCloseTo(34.51, 1);
  });

  test('cost=0 不算剩余价值（免费机器没有剩余价值可言）', () => {
    expect(remainingValue({ id: 'x', cost: 0, cycle: 'yearly', days_left: 89 })).toBeNull();
  });

  test('无到期日 → null', () => {
    expect(remainingValue({ id: 'x', cost: 79, cycle: 'yearly' })).toBeNull();
  });

  test('onetime / topup 不摊日单价（没有周期概念）', () => {
    expect(remainingValue({ id: 'x', cost: 79, cycle: 'onetime', days_left: 30 })).toBeNull();
    expect(remainingValue({ id: 'x', cost: 79, cycle: 'topup', days_left: 30 })).toBeNull();
  });

  test('已过期（days_left 负）不给负价值', () => {
    expect(remainingValue({ id: 'x', cost: 79, cycle: 'yearly', days_left: -3 })).toBeNull();
  });
});

describe('uptimeText / formatRate / formatMbPair', () => {
  test('在线天数；不足一天降级到小时，不显「在线 0 天」', () => {
    expect(uptimeText(797822)).toBe('在线 9 天');
    expect(uptimeText(7200)).toBe('在线 2 小时');
    expect(uptimeText(90)).toBe('在线 1 分钟');
    expect(uptimeText(undefined)).toBeNull();
  });

  test('速率分档 B/K/M（参考图口径）', () => {
    expect(formatRate(520)).toBe('520 B/s');
    expect(formatRate(1653)).toBe('1.6 K/s');
    expect(formatRate(303294)).toBe('296.2 K/s');
    expect(formatRate(2 * 1024 * 1024)).toBe('2.0 M/s');
    expect(formatRate(null)).toBe('—');
  });

  test('内存两侧同单位，单位由总量决定（不出混单位行）', () => {
    expect(formatMbPair(327.7, 942.1)).toBe('327.7 MB / 942.1 MB');
    expect(formatMbPair(2440.2, 3723.2)).toBe('2.4 GB / 3.6 GB');
    // 已用不足 1 GB 但总量过线时，仍按总量单位对齐
    expect(formatMbPair(632.1, 23974.9)).toBe('0.6 GB / 23.4 GB');
    expect(formatMbPair(undefined, 942.1)).toBeUndefined();
  });
});

describe('meterTone / statusTone', () => {
  test('90/70 阈值（D2）', () => {
    expect(meterTone(95)).toBe('bg-signal-5');
    expect(meterTone(90)).toBe('bg-signal-5');
    expect(meterTone(70)).toBe('bg-signal-4');
    expect(meterTone(69.9)).toBe('bg-signal-1');
  });

  test('状态点走 signal 语义色（green-500 对玻璃底只有 2.15:1）', () => {
    expect(statusTone('up')).toBe('bg-signal-1');
    expect(statusTone('down')).toBe('bg-signal-5');
    expect(statusTone('unknown')).toBe('bg-muted-foreground');
    expect(statusTone(undefined)).toBe('bg-muted-foreground');
  });
});

describe('MachineCard 状态点按 role 取字段（§4.3）', () => {
  const sys: MonitorSystem = { id: 's', name: 'X', status: 'up' };

  test('node 读 probe_status，不看 system.status', () => {
    const { container } = render(
      <MachineCard machine={{ ...KR, probe_status: 'down' }} system={sys} />
    );
    expect(container.querySelector('span[title="down"]')).not.toBeNull();
  });

  test('server 读 system.status，不看 probe_status', () => {
    const { container } = render(
      <MachineCard machine={{ ...KR, role: 'server', probe_status: 'down' }} system={sys} />
    );
    expect(container.querySelector('span[title="up"]')).not.toBeNull();
  });
});

describe('MachineCard 整卡可点（契约 1 的 onClick）', () => {
  test('不传 onClick 时不加 role/tabIndex，也不给 cursor-pointer', () => {
    const { container } = render(<MachineCard machine={KR} />);
    const card = container.firstElementChild!;
    expect(card.getAttribute('role')).toBeNull();
    expect(card.getAttribute('tabindex')).toBeNull();
    expect(card.className).not.toContain('cursor-pointer');
  });

  test('传 onClick 时点击触发，且键盘可达（Enter / 空格）', () => {
    const onClick = vi.fn();
    const { container } = render(<MachineCard machine={KR} onClick={onClick} />);
    const card = container.firstElementChild!;
    expect(card.getAttribute('role')).toBe('button');
    expect(card.getAttribute('tabindex')).toBe('0');

    fireEvent.click(card);
    fireEvent.keyDown(card, { key: 'Enter' });
    fireEvent.keyDown(card, { key: ' ' });
    expect(onClick).toHaveBeenCalledTimes(3);
  });

  test('动作按钮不冒泡成整卡跳转', () => {
    const onClick = vi.fn();
    render(
      <MachineCard
        machine={{ ...KR, actions: [{ key: 'restart-singbox', label: '重启 sing-box' }] }}
        onClick={onClick}
      />
    );
    // 整卡自己也是 role=button 且可访问名含内部文字，所以按名字查会同时命中卡片；
    // 取 name 完全等于动作标签的那个。
    fireEvent.click(screen.getByRole('button', { name: '重启 sing-box' }));
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe('MachineCard 色带行（§4.3：无记录整行不渲染）', () => {
  test('无 probe_history 时不出现延迟/失败率两块', () => {
    render(<MachineCard machine={KR} />);
    expect(screen.queryByText('延迟')).toBeNull();
    expect(screen.queryByText('探测失败率')).toBeNull();
  });

  test('空数组也不渲染（不拿 0% 冒充一切正常）', () => {
    render(<MachineCard machine={{ ...KR, probe_history: [] }} />);
    expect(screen.queryByText('探测失败率')).toBeNull();
  });

  test('有记录时两块都出现', () => {
    render(
      <MachineCard
        machine={{ ...KR, probe_history: [{ ts: 't1', status: 'up', latency_ms: 212 }] }}
      />
    );
    expect(screen.getByText('延迟')).toBeTruthy();
    expect(screen.getByText('探测失败率')).toBeTruthy();
  });
});

describe('publicProbeText（家宽公网状态文案）', () => {
  test('up 取最近一次耗时；没有耗时样本时退回「可达」', () => {
    expect(publicProbeText('up', [{ ts: 't1', status: 'up', latency_ms: 181.2 }])).toBe('181.2 ms');
    expect(publicProbeText('up', [])).toBe('可达');
    expect(publicProbeText('up', [{ ts: 't1', status: 'down', latency_ms: null }])).toBe('可达');
  });

  test('down 恒为「不通」，其它（含本机代理没放行）恒为「未知」', () => {
    expect(publicProbeText('down', [{ ts: 't1', status: 'up', latency_ms: 170 }])).toBe('不通');
    expect(publicProbeText('unknown', [{ ts: 't1', status: 'up', latency_ms: 170 }])).toBe('未知');
    expect(publicProbeText(undefined)).toBe('未知');
  });
});

describe('MachineCard 家宽公网行（与 Tailscale 探活分开看）', () => {
  const US: Machine = {
    ...KR,
    id: 'us',
    label: 'Oracle',
    role: 'server',
    public_host: 'us.sunxc.eu.cc',
    probe: { type: 'tcp', port: 22 },
    public_probe: { type: 'tcp', port: 62037 },
  };

  test('没配 public_probe 时整行与 chip 都不出现', () => {
    render(
      <MachineCard
        machine={{ ...KR, public_probe_status: 'down', public_probe_msg: 'stale' }}
      />
    );
    expect(screen.queryByText(/家宽公网/)).toBeNull();
  });

  test('配了但还没有任何结论（无样本无说明）时整行不渲染', () => {
    render(<MachineCard machine={{ ...US, public_probe_status: 'unknown', public_probe_history: [] }} />);
    expect(screen.queryByText(/家宽公网/)).toBeNull();
  });

  test('up：显示端口与最近耗时，不出 chip', () => {
    render(
      <MachineCard
        machine={{
          ...US,
          public_probe_status: 'up',
          public_probe_msg: 'tcp us.sunxc.eu.cc:62037 ok 181.2ms',
          public_probe_history: [{ ts: 't1', status: 'up', latency_ms: 181.2 }],
        }}
      />
    );
    expect(screen.getByText('家宽公网 · tcp 62037')).toBeTruthy();
    expect(screen.getByText('181.2 ms')).toBeTruthy();
    expect(screen.queryByText('家宽公网不通')).toBeNull();
  });

  test('down：行内显示「不通」，顶部出 warn chip，悬停带探测说明', () => {
    render(
      <MachineCard
        machine={{
          ...US,
          probe_status: 'up',
          public_probe_status: 'down',
          public_probe_msg: 'tcp us.sunxc.eu.cc:62037 timed out',
          public_probe_history: [
            { ts: 't1', status: 'up', latency_ms: 170 },
            { ts: 't2', status: 'down', latency_ms: null },
          ],
        }}
      />
    );
    expect(screen.getByText('不通')).toBeTruthy();
    const chip = screen.getByText('家宽公网不通');
    expect(chip.getAttribute('title')).toBe('tcp us.sunxc.eu.cc:62037 timed out');
    expect(chip.className).toContain('amber');
  });

  test('本机代理没放行（unknown + 说明）：显示「未知」而不是「可达」，不出 chip', () => {
    const msg = 'tcp us.sunxc.eu.cc:62037 握手 0.4ms，快于公网下限，疑似本机代理代答';
    const { container } = render(
      <MachineCard
        machine={{
          ...US,
          public_probe_status: 'unknown',
          public_probe_msg: msg,
          public_probe_history: [{ ts: 't1', status: 'up', latency_ms: 170 }],
        }}
      />
    );
    expect(screen.getByText('未知')).toBeTruthy();
    expect(screen.queryByText('家宽公网不通')).toBeNull();
    expect(container.querySelector(`[title="${msg}"]`)).not.toBeNull();
  });
});

describe('MachineCard 2×2 副行按行留位（§4.3）', () => {
  /** 副行是 text-[11px]，按 class 数比按文本稳（占位副行的文本是 U+00A0） */
  const subCount = (c: HTMLElement) => c.querySelectorAll('span.text-\\[11px\\].truncate').length;

  test('无 system 时四格副行全不渲染，不留四行死白', () => {
    const { container } = render(<MachineCard machine={KR} />);
    expect(subCount(container)).toBe(0);
  });

  test('同行一格有数据，另一格留空占位，两格的条不错位', () => {
    // 第一行：CPU 有 load、内存无 used/total → 两格都留位
    // 第二行：磁盘无、流量无 → 整行不留
    const { container } = render(
      <MachineCard machine={KR} system={{ cpu: 0.2, load_avg: [0.1, 0.2, 0.3] } as MonitorSystem} />
    );
    expect(subCount(container)).toBe(2);
  });

  test('四格都有数据时四行副行齐全', () => {
    const { container } = render(
      <MachineCard
        machine={{
          ...KR,
          traffic: {
            month: '2026-08',
            nic: 'eth0',
            sent_gb: 12.5,
            recv_gb: 40,
            used_gb: 12.5,
            quota_gb: 0,
            pct: null,
          },
        }}
        system={
          {
            cpu: 1,
            load_avg: [0.1, 0.2, 0.3],
            memory_used_mb: 512,
            memory_total_mb: 2048,
            disk_used_gb: 10,
            disk_total_gb: 40,
          } as MonitorSystem
        }
      />
    );
    expect(subCount(container)).toBe(4);
  });
});

describe('MachineCard 剩余价值列跟着币种走', () => {
  test('USD 机器的剩余价值用 $，不混出一个 ¥', () => {
    render(
      <MachineCard
        machine={{
          ...KR,
          renewal: { id: 'us', cost: 5, currency: 'USD', cycle: 'monthly', days_left: 3 },
        }}
      />
    );
    expect(screen.getByText('$5 / 月')).toBeTruthy();
    expect(screen.getByText('$0.50')).toBeTruthy();
    expect(screen.queryByText('¥0.50')).toBeNull();
  });

  test('CNY 机器两处都是 ¥', () => {
    render(
      <MachineCard
        machine={{
          ...KR,
          renewal: { id: 'txy', cost: 79, currency: 'CNY', cycle: 'yearly', days_left: 556 },
        }}
      />
    );
    expect(screen.getByText('¥79 / 年')).toBeTruthy();
    expect(screen.getByText('¥120.34')).toBeTruthy();
  });
});

describe('MachineCard 底部标识（§4.3）', () => {
  test('public_host + 探测端口合成一行', () => {
    render(<MachineCard machine={KR} />);
    expect(screen.getByText('kr.sunxc.eu.cc · tcp 62037')).toBeTruthy();
  });

  test('无 public_host 时回落 system.host（服务器走 Beszel 上报地址）', () => {
    render(
      <MachineCard
        machine={{ id: 'txy', label: 'TXY', role: 'server' }}
        system={{ id: 's', name: 'TXY', host: '100.64.0.30' }}
      />
    );
    expect(screen.getByText('100.64.0.30')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// 以下原属 node-card.test.tsx（E1-E4）。I0-2 删掉 NodeMachineCard 薄包装后，
// 这些断言直接打在 MachineCard 上——覆盖面不变，只是少了一层转发。
// ---------------------------------------------------------------------------

const KR_FULL: Machine = {
  ...KR,
  provider: 'AWS Lightsail 韩国',
  probe_msg: 'tcp kr.sunxc.eu.cc:62037 ok',
  probe_ts: '2026-08-29T05:00:00Z',
  renewal: { id: 'kr-renewal', expires_at: '2026-11-26', days_left: 89 },
};

describe('MachineCard node 侧 header 与台账退化（§4.1 / §4.3）', () => {
  test('渲染 label / 底部标识 public_host+探测端口 / 剩余天数', () => {
    render(<MachineCard machine={KR_FULL} />);
    expect(screen.getByText('光帆 KR')).toBeTruthy();
    expect(screen.getByText(/kr\.sunxc\.eu\.cc · tcp 62037/)).toBeTruthy();
    expect(screen.getByText('剩余 89 天')).toBeTruthy();
  });

  test('header 让位给 OS/地区；provider 与 probe_ts 不再占正文行', () => {
    render(<MachineCard machine={{ ...KR_FULL, os: 'Debian 13', region: '🇰🇷' }} />);
    expect(screen.getByText('Debian 13')).toBeTruthy();
    expect(screen.getByText('🇰🇷')).toBeTruthy();
    // §4.1 header 只有状态点 + 名称 + OS + 旗；provider 一整行被删掉了
    expect(screen.queryByText('AWS Lightsail 韩国')).toBeNull();
    expect(screen.queryByText(/2026-08-29T05:00:00Z/)).toBeNull();
  });

  test('无 os/region 时 header 右侧整块不渲染（§4.3）', () => {
    const { container } = render(<MachineCard machine={KR_FULL} />);
    expect(container.querySelector('.lucide-monitor-smartphone')).toBeNull();
  });

  test('无 renewal 时显示「长期」而不是 0 天（§4.3 退化）', () => {
    render(
      <MachineCard machine={{ ...KR_FULL, renewal: null, probe: undefined, probe_ts: undefined }} />
    );
    expect(screen.queryByText(/89 天/)).toBeNull();
    expect(screen.getByText('长期')).toBeTruthy();
  });

  test('cost 缺失不等于免费：价格 chip 与剩余价值都不出现（§4.3）', () => {
    render(<MachineCard machine={KR_FULL} />);
    expect(screen.queryByText('免费')).toBeNull();
    expect(screen.queryByText(/^¥/)).toBeNull();
  });

  test('cost=0 显示「免费」chip，且剩余价值也是免费（§3.3）', () => {
    render(
      <MachineCard
        machine={{
          ...KR_FULL,
          renewal: { id: 'node-kr', days_left: 89, cost: 0, currency: 'CNY', cycle: 'yearly' },
        }}
      />
    );
    expect(screen.getAllByText('免费').length).toBe(2); // chip + 剩余价值列
  });
});

describe('MachineCard 指标组（E2）', () => {
  const SYSTEM: MonitorSystem = {
    id: 's-kr',
    name: 'KR',
    machine: 'kr',
    status: 'up',
    cpu: 12.3,
    memory_pct: 45.1,
    memory_total_mb: 1912.6,
    memory_used_mb: 862.4,
    net_nic: 'ens5',
    net_up_bytes_sec: 516,
    net_down_bytes_sec: 2048,
  };

  test('有 system 时四格 + 速率列都有值', () => {
    render(<MachineCard machine={KR_FULL} system={SYSTEM} />);
    expect(screen.getByText('12.3%')).toBeTruthy();
    expect(screen.getByText('45.1%')).toBeTruthy();
    expect(screen.getByText('0.8 GB / 1.9 GB')).toBeTruthy();
    // 速率拆成两行（§4.1 细节行第一列），不再挤成一个 ↑↓ 串
    expect(screen.getByText('516 B/s')).toBeTruthy();
    expect(screen.getByText('2.0 K/s')).toBeTruthy();
  });

  test('load_avg 三段作 CPU 副行（§4.1）', () => {
    render(<MachineCard machine={KR_FULL} system={{ ...SYSTEM, load_avg: [0.01, 0.01, 0] }} />);
    expect(screen.getByText('0.01, 0.01, 0.00')).toBeTruthy();
  });

  test('节点卡也显示硬盘（§4.1 定案：两类 role 同一套 2×2）', () => {
    render(
      <MachineCard
        machine={KR_FULL}
        system={{ ...SYSTEM, disk_pct: 3.7, disk_used_gb: 1.4, disk_total_gb: 39.1 }}
      />
    );
    expect(screen.getByText('硬盘')).toBeTruthy();
    expect(screen.getByText('1.4 GB / 39.1 GB')).toBeTruthy();
  });

  test('温度不占格子，只进 CPU 格 hover（§3.1：五台皆空）', () => {
    const { container } = render(
      <MachineCard machine={KR_FULL} system={{ ...SYSTEM, temperature: 60 }} />
    );
    expect(screen.queryByText('温度')).toBeNull();
    expect(container.querySelector('[title="温度 60 °C"]')).not.toBeNull();
  });

  test('container_count 不展示（§3.1：Beszel info.ct 不可信）', () => {
    render(<MachineCard machine={KR_FULL} system={{ ...SYSTEM, container_count: 2 }} />);
    expect(screen.queryByText('容器')).toBeNull();
  });

  test('无 system（agent 未就绪）时 2×2 仍在位，逐格退化为 —', () => {
    render(<MachineCard machine={KR_FULL} />);
    // 格子恒在：六张卡高度对齐比「没数据就抽掉」重要（§4.3 是逐格退化，不是整组退化）
    expect(screen.getByText('CPU')).toBeTruthy();
    expect(screen.getByText('内存')).toBeTruthy();
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(4);
  });

  test('月流量渲染已用/配额与上下行累计（E1）', () => {
    render(
      <MachineCard
        machine={{
          ...KR_FULL,
          traffic: {
            month: '2026-08',
            nic: 'ens5',
            // 配额只算出站（Q4 拍板）：used_gb == sent_gb，不含 recv_gb
            sent_gb: 100,
            recv_gb: 60,
            used_gb: 100,
            quota_gb: 1000,
            pct: 10,
            since: '2026-08-29T00:00:00Z',
            resets: 0,
          },
        }}
      />
    );
    expect(screen.getByText('100.0 GB / 1000.0 GB')).toBeTruthy();
    expect(screen.getByText('10.0%')).toBeTruthy();
    // 累计列两行分开（§4.1 细节行第二列）
    expect(screen.getByText('60.0 GB')).toBeTruthy();
  });

  test('无配额（nas 路径）只显本月累计，不画条不告警（契约 4）', () => {
    const { container } = render(
      <MachineCard
        machine={{
          ...KR_FULL,
          traffic: {
            month: '2026-08',
            nic: 'enp1s0',
            sent_gb: 12.5,
            recv_gb: 30.1,
            used_gb: 12.5,
            quota_gb: 0,
            pct: null,
          },
        }}
      />
    );
    // 两处都是 12.5：Q4 拍板后配额只算出站，used_gb 与 sent_gb 同值，
    // 流量格主值与累计列上行本来就该一致（累计列的新信息是下行 30.1）
    expect(screen.getAllByText('12.5 GB').length).toBe(2);
    expect(screen.getByText('30.1 GB')).toBeTruthy();
    expect(screen.getByText('本月累计 · 2026-08')).toBeTruthy();
    // 填充条是唯一带内联 width 的元素，比按 bg-signal-* 找准确（状态点也是 signal 色）
    expect(container.querySelectorAll('div[style*="width"]').length).toBe(0);
  });

  test('无 traffic 时流量格仍在位但显示 —', () => {
    render(<MachineCard machine={{ ...KR_FULL, traffic: null }} />);
    expect(screen.getByText('流量')).toBeTruthy();
  });
});

describe('MachineCard 白名单动作（E3）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const withAction: Machine = {
    ...KR_FULL,
    actions: [{ key: 'restart-singbox', label: '重启 sing-box' }],
  };

  test('无 actions 时不渲染任何动作按钮', () => {
    render(<MachineCard machine={KR_FULL} />);
    expect(screen.queryByRole('button')).toBeNull();
  });

  test('点按钮先弹确认，取消不发请求', async () => {
    render(<MachineCard machine={withAction} />);
    fireEvent.click(screen.getByRole('button', { name: '重启 sing-box' }));
    expect(await screen.findByText(/确认在 光帆 KR 上执行/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(runMachineAction).not.toHaveBeenCalled();
  });

  test('确认后只送 machine id 与 action key', async () => {
    vi.mocked(runMachineAction).mockResolvedValue({ ok: true, output: 'ok' });
    const onActionDone = vi.fn();
    render(<MachineCard machine={withAction} onActionDone={onActionDone} />);
    fireEvent.click(screen.getByRole('button', { name: '重启 sing-box' }));
    fireEvent.click(await screen.findByRole('button', { name: '执行' }));
    await waitFor(() => expect(runMachineAction).toHaveBeenCalledWith('kr', 'restart-singbox'));
    await waitFor(() => expect(onActionDone).toHaveBeenCalled());
  });

  test('失败时不触发刷新回调', async () => {
    vi.mocked(runMachineAction).mockResolvedValue({ ok: false, output: 'unit not found' });
    const onActionDone = vi.fn();
    render(<MachineCard machine={withAction} onActionDone={onActionDone} />);
    fireEvent.click(screen.getByRole('button', { name: '重启 sing-box' }));
    fireEvent.click(await screen.findByRole('button', { name: '执行' }));
    await waitFor(() => expect(runMachineAction).toHaveBeenCalled());
    expect(onActionDone).not.toHaveBeenCalled();
  });
});

describe('projectionText / 预计超配额 chip（M1）', () => {
  const base: MachineTraffic = {
    month: '2026-09',
    nic: 'ens5',
    sent_gb: 900,
    recv_gb: 120,
    used_gb: 900,
    quota_gb: 1000,
    pct: 90,
    since: '2026-09-01T00:00:00Z',
  };
  const over = {
    projected_gb: 1350,
    daily_gb: 45,
    observed_hours: 480,
    remaining_days: 10,
    partial_month: false,
    quota_gb: 1000,
    projected_pct: 135,
    over_quota: true,
  };

  test('没有 projection → null：观测不足 72 小时时什么都不说，不退化成「预计 0 GB」', () => {
    expect(projectionText(base)).toBeNull();
    expect(projectionText({ ...base, projection: null })).toBeNull();
    expect(projectionText(null)).toBeNull();
    expect(projectionText(undefined)).toBeNull();
  });

  test('有配额时带上占比', () => {
    expect(projectionText({ ...base, projection: over })).toBe(
      '按 45 GB/天推算，本月预计 1350 GB / 1000 GB（135%）'
    );
  });

  test('无配额只说绝对量，不编一个百分比', () => {
    const text = projectionText({
      ...base,
      quota_gb: null,
      pct: null,
      projection: { ...over, quota_gb: null, projected_pct: null, over_quota: false },
    });
    expect(text).toBe('按 45 GB/天推算，本月预计 1350 GB');
  });

  test('partial_month 必须说明「从首次采样起算」（设计 §5.7）', () => {
    const text = projectionText({
      ...base,
      since: '2026-09-03T10:00:00Z',
      projection: { ...over, partial_month: true },
    });
    expect(text).toContain('从首次采样 2026-09-03T10:00:00Z 起算，非整月');
  });

  test('over_quota 才出 chip；不超时整卡没有这个词', () => {
    const { rerender } = render(
      <MachineCard machine={{ ...KR, traffic: { ...base, projection: over } }} />
    );
    expect(screen.getByText('预计超配额')).toBeTruthy();

    rerender(
      <MachineCard
        machine={{ ...KR, traffic: { ...base, projection: { ...over, over_quota: false } } }}
      />
    );
    expect(screen.queryByText('预计超配额')).toBeNull();
  });
});
