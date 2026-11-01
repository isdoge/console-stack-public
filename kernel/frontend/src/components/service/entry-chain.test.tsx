import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { EntryChain, domainHost } from './entry-chain';
import type { ServiceEntry } from '@/lib/types';

/**
 * C5 → L4 入口维度：链路展示与三态边缘。守两条底线——
 * ① 边缘数据源不可用时显式「未知」，不假装直连；② 组件只吃后端投影，
 * 内网地址根本不在数据里（后端 test_entry_view 那侧另有断言）。
 */

function entry(over: Partial<ServiceEntry> = {}): ServiceEntry {
  return {
    domain: 'https://sub.example.test',
    edge: 'caddy',
    edge_known: true,
    tailscale: true,
    home: false,
    layers: ['domain', 'edge', 'tailscale'],
    ...over,
  };
}

describe('domainHost', () => {
  test('只取主机名，去掉 scheme 与路径', () => {
    expect(domainHost('https://sub.example.test/path')).toBe('sub.example.test');
    expect(domainHost('sub.example.test')).toBe('sub.example.test');
    expect(domainHost('https://a.b.c:8443')).toBe('a.b.c:8443');
  });

  test('空值与非法值不炸', () => {
    expect(domainHost(null)).toBeNull();
    expect(domainHost('   ')).toBeNull();
    expect(domainHost('::::')).toBe('::::');
  });
});

describe('EntryChain', () => {
  test('完整链路：域名（可点）› 边缘 › tailnet', () => {
    render(<EntryChain entry={entry()} />);
    const link = screen.getByRole('link', { name: 'sub.example.test' });
    expect(link.getAttribute('href')).toBe('https://sub.example.test');
    expect(screen.getByText('caddy')).toBeTruthy();
    expect(screen.getByText('tailnet')).toBeTruthy();
  });

  test('不在 upstream 名单里就没有边缘层，但也不显示「未知」', () => {
    render(<EntryChain entry={entry({ edge: null, layers: ['domain', 'tailscale'] })} />);
    expect(screen.queryByText('caddy')).toBeNull();
    expect(screen.queryByText('边缘未知')).toBeNull();
    expect(screen.getByText('tailnet')).toBeTruthy();
  });

  test('C11 快照不可用 → 有域名时显式标「边缘未知」，不装作直连', () => {
    render(
      <EntryChain entry={entry({ edge: null, edge_known: false, layers: ['domain', 'tailscale'] })} />
    );
    expect(screen.getByText('边缘未知')).toBeTruthy();
  });

  test('纯内网服务只显示内网层，不因为没域名就说未知', () => {
    render(
      <EntryChain
        entry={entry({ domain: null, edge: null, edge_known: false, tailscale: true, home: true, layers: ['tailscale', 'home'] })}
      />
    );
    expect(screen.getByText('tailnet')).toBeTruthy();
    expect(screen.getByText('内网')).toBeTruthy();
    expect(screen.queryByText('边缘未知')).toBeNull();
  });

  test('台账没登记任何入口 / 后端没给 entry → 一个短横线', () => {
    const { container: a } = render(
      <EntryChain
        entry={entry({ domain: null, edge: null, edge_known: true, tailscale: false, home: false, layers: [] })}
      />
    );
    expect(a.textContent).toBe('-');
    const { container: b } = render(<EntryChain />);
    expect(b.textContent).toBe('-');
  });
});
