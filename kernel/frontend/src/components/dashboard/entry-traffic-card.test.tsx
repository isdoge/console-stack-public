import { displayTime } from '@/lib/display-time';
import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  EntryTrafficCard,
  compactCount,
  failsTone,
  hostLabel,
  requestsText,
} from './entry-traffic-card';
import type { EntryTrafficStatus } from '@/lib/types';

/**
 * C11 计数口径（2026-09-07 复核）：`num_requests` 是**瞬时活跃请求数**、
 * `fails` 是被动健康检查记住的失败数——两者都不是累计量，全 0 是常态。
 * 这组用例守的是「卡片不把瞬时量说成流量统计」，证据链见 `caddy_view.py` 模块 docstring。
 */

function status(over: Partial<EntryTrafficStatus> = {}): EntryTrafficStatus {
  return {
    available: true,
    sampled_at: '2026-09-07T00:00:00Z',
    error: null,
    upstreams: [{ id: 'upstream-1', name: 'sublinkpro', num_requests: 0, fails: 0 }],
    metrics_available: false,
    metrics_error: null,
    counting_since: null,
    hosts: [],
    ...over,
  };
}

describe('requestsText / failsTone', () => {
  test('0 是合法读数（空闲），只有 null 才是 —', () => {
    expect(requestsText(0)).toBe('0');
    expect(requestsText(12)).toBe('12');
    expect(requestsText(null)).toBe('—');
  });

  test('失败数 >0 才染红，0 与 null 都弱化', () => {
    expect(failsTone(1)).toBe('text-signal-5');
    expect(failsTone(0)).toBe('text-muted-foreground');
    expect(failsTone(null)).toBe('text-muted-foreground');
  });
});

describe('EntryTrafficCard 口径文案', () => {
  test('标题是「入口上游」，不是会被读成流量统计的「入口流量」', () => {
    render(<EntryTrafficCard status={status()} />);
    expect(screen.getByText('入口上游')).toBeTruthy();
    expect(screen.queryByText('入口流量')).toBeNull();
  });

  test('数值带「活跃 / 失败」前缀，并写明是瞬时快照', () => {
    render(<EntryTrafficCard status={status()} />);
    expect(screen.getByText('活跃 0')).toBeTruthy();
    expect(screen.getByText('失败 0')).toBeTruthy();
    expect(screen.getByText(/瞬时快照/)).toBeTruthy();
    expect(screen.getByText(/非累计流量/)).toBeTruthy();
  });

  test('两个数值的 tooltip 分别说清 in-flight 与被动健康检查口径', () => {
    render(<EntryTrafficCard status={status()} />);
    expect(screen.getByTitle(/in-flight/)).toBeTruthy();
    expect(screen.getByTitle(/passive health checks/)).toBeTruthy();
  });

  test('全 0 仍按正常快照渲染，不显示降级文案', () => {
    render(
      <EntryTrafficCard
        status={status({
          upstreams: [
            { id: 'upstream-1', name: 'sublinkpro', num_requests: 0, fails: 0 },
            { id: 'upstream-2', name: 'upstream-2', num_requests: 0, fails: 0 },
          ],
        })}
      />
    );
    expect(screen.getAllByText('活跃 0').length).toBe(2);
    expect(screen.queryByText('数据不可用')).toBeNull();
    expect(screen.queryByText('暂无入口流量数据')).toBeNull();
  });

  test('导出文件缺失 → 显式降级态，不是全 0 假数据', () => {
    render(
      <EntryTrafficCard
        status={status({
          available: false,
          error: 'Caddy 导出文件不存在（cron 未就绪或挂载缺失）',
          upstreams: [],
        })}
      />
    );
    expect(screen.getByText('数据不可用')).toBeTruthy();
    expect(screen.getByText(/Caddy 导出文件不存在/)).toBeTruthy();
    expect(screen.queryByText(/瞬时快照/)).toBeNull();
  });
});

/**
 * 累计量分区（2026-09-07 owner 授权补齐）。守两件事：
 * ① 数值必须连同「自 X 起」的起点一起出现——Caddy 计数器每次 reload 归零，
 *    只给数字会被读成开天辟地以来的总量；
 * ② 两条数据链各自降级——指标坏了不能把瞬时量那半边也拖没。
 */
describe('compactCount / hostLabel', () => {
  test('万级以下原样，万级起走 k/M', () => {
    expect(compactCount(0)).toBe('0');
    expect(compactCount(9999)).toBe('9999');
    expect(compactCount(10_000)).toBe('10.0k');
    expect(compactCount(1_234_567)).toBe('1.2M');
  });

  test('_other 不能裸露给用户（会被当成一台叫 _other 的机器）', () => {
    expect(hostLabel('_other')).toBe('其它（Host 不匹配）');
    expect(hostLabel('axonhub.example.test')).toBe('axonhub.example.test');
  });
});

describe('EntryTrafficCard 累计量分区', () => {
  const withMetrics = status({
    metrics_available: true,
    counting_since: '2026-09-07T07:39:46Z',
    hosts: [
      {
        host: 'axonhub2.example.test',
        requests_total: 40,
        ok_2xx: 38,
        redirect_3xx: 0,
        client_4xx: 0,
        server_5xx: 2,
      },
      {
        host: '_other',
        requests_total: 7,
        ok_2xx: 0,
        redirect_3xx: 0,
        client_4xx: 7,
        server_5xx: 0,
      },
    ],
  });

  test('渲染域名与累计数', () => {
    render(<EntryTrafficCard status={withMetrics} />);
    expect(screen.getByText('按域名累计')).toBeTruthy();
    expect(screen.getByText('axonhub2.example.test')).toBeTruthy();
    expect(screen.getByText('40')).toBeTruthy();
  });

  test('上游源过期移除旧计数与服务名，指标仍独立可用', () => {
    const upstreams_sampled_at = '2026-09-08T11:45:00.125Z';
    const fresh = { ...withMetrics, upstreams_sampled_at };
    const { rerender } = render(<EntryTrafficCard status={fresh} />);
    expect(screen.getByText(displayTime(upstreams_sampled_at).text)).toBeTruthy();
    expect(screen.getByText('活跃 0')).toBeTruthy();
    rerender(<EntryTrafficCard status={{
      ...fresh, available: false, error: 'Caddy 上游数据已过期（超过 15 分钟未更新）',
    }} />);
    expect(screen.queryByText('活跃 0')).toBeNull();
    expect(screen.queryByText('失败 0')).toBeNull();
    expect(screen.queryByText('sublinkpro')).toBeNull();
    expect(screen.getByText(/上游数据已过期/)).toBeTruthy();
    expect(screen.getByText(displayTime(upstreams_sampled_at).text)).toBeTruthy();
    expect(screen.getByText('axonhub2.example.test')).toBeTruthy();
    expect(screen.getByText('40')).toBeTruthy();
  });

  test('指标时间独立显示，过期后旧数字与计数起点消失', () => {
    const metrics_sampled_at = '2026-09-07T08:00:00Z';
    const { rerender } = render(<EntryTrafficCard status={{ ...withMetrics, metrics_sampled_at }} />);
    expect(screen.getByText(displayTime(metrics_sampled_at).text)).toBeTruthy();
    expect(screen.getByText('40')).toBeTruthy();
    const metrics_error = 'Caddy 指标数据已过期（超过 15 分钟未更新）';
    rerender(<EntryTrafficCard status={{
      ...withMetrics, metrics_sampled_at, metrics_available: false, metrics_error,
    }} />);
    expect(screen.getByText(metrics_error)).toBeTruthy();
    expect(screen.queryByText('40')).toBeNull();
    expect(screen.queryByText('axonhub2.example.test')).toBeNull();
    expect(screen.queryByText(/自 .* 起/)).toBeNull();
    expect(screen.getByText('sublinkpro')).toBeTruthy();
    expect(screen.getByText(displayTime(metrics_sampled_at).text)).toBeTruthy();
  });

  test('旧响应不杜撰指标采集时间', () => {
    render(<EntryTrafficCard status={withMetrics} />);
    expect(screen.queryByText(/指标采集：/)).toBeNull();
    expect(screen.queryByText(/上游采集：/)).toBeNull();
  });

  test('计数起点必须显示——否则数值会被当成历史总量', () => {
    render(<EntryTrafficCard status={withMetrics} />);
    expect(screen.getByText(/自 2026-09-07T07:39:46Z 起/)).toBeTruthy();
  });

  test('_other 显示为人话而不是裸下划线名', () => {
    render(<EntryTrafficCard status={withMetrics} />);
    expect(screen.getByText('其它（Host 不匹配）')).toBeTruthy();
    expect(screen.queryByText('_other')).toBeNull();
  });

  test('指标降级时只塌掉累计分区，瞬时量照常渲染', () => {
    render(
      <EntryTrafficCard
        status={status({
          metrics_available: false,
          metrics_error: 'Caddy 指标文件不存在（cron 未就绪或挂载缺失）',
        })}
      />,
    );
    expect(screen.getByText('sublinkpro')).toBeTruthy();
    expect(screen.getByText(/指标文件不存在/)).toBeTruthy();
  });

  test('瞬时量降级时累计分区仍能独立渲染', () => {
    render(
      <EntryTrafficCard
        status={status({
          available: false,
          error: 'Caddy 导出文件不存在（cron 未就绪或挂载缺失）',
          upstreams: [],
          metrics_available: true,
          counting_since: '2026-09-07T07:39:46Z',
          hosts: [
            {
              host: 'axonhub.example.test',
              requests_total: 15,
              ok_2xx: 14,
              redirect_3xx: 0,
              client_4xx: 1,
              server_5xx: 0,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/导出文件不存在/)).toBeTruthy();
    expect(screen.getByText('axonhub.example.test')).toBeTruthy();
    expect(screen.getByText('15')).toBeTruthy();
  });
});
