import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  formatProxyFrontend,
  formatProxySource,
  ReverseProxyRoutes,
} from './reverse-proxy-routes';
import type { ReverseProxyRoute, Service } from '@/lib/types';

const route: ReverseProxyRoute = {
  listen: [':3000'],
  hosts: [],
  paths: ['/static/*', '/assets/*'],
  upstreams: ['new-api:3000'],
};

describe('formatProxyFrontend', () => {
  test('同时展示域名、监听和默认路径', () => {
    expect(
      formatProxyFrontend({
        listen: [':443'],
        hosts: ['api.example.test'],
        paths: [],
        upstreams: ['app:8080'],
      })
    ).toBe('api.example.test (:443) /*');
  });

  test('无域名时展示监听地址和路径', () => {
    expect(formatProxyFrontend(route)).toBe(':3000 /static/*, /assets/*');
  });
});

describe('formatProxySource', () => {
  test('转换固定来源标签并保留未知来源', () => {
    expect(formatProxySource('host')).toBe('宿主机');
    expect(formatProxySource('container')).toBe('容器');
    expect(formatProxySource('custom')).toBe('custom');
    expect(formatProxySource()).toBeNull();
  });
});

describe('ReverseProxyRoutes', () => {
  test('渲染所有反代前置与上游', () => {
    const svc: Service = {
      name: 'new-api-proxy',
      machine: 'aly',
      proxy_routes: [
        { ...route, source: 'container' },
        {
          listen: [':443'],
          hosts: ['newapi.example.test'],
          paths: [],
          upstreams: ['100.64.0.30:8090'],
          source: 'host',
        },
      ],
    };
    render(<ReverseProxyRoutes svc={svc} />);
    expect(screen.getByText(':3000 /static/*, /assets/*')).toBeTruthy();
    expect(screen.getByText('newapi.example.test (:443) /*')).toBeTruthy();
    expect(screen.getByText('new-api:3000')).toBeTruthy();
    expect(screen.getByText('100.64.0.30:8090')).toBeTruthy();
    expect(screen.getByText('容器')).toBeTruthy();
    expect(screen.getByText('宿主机')).toBeTruthy();
  });

  test('采集失败显示降级提示', () => {
    render(
      <ReverseProxyRoutes
        svc={{ name: 'proxy', machine: 'aly', proxy_routes: [], proxy_error: 'timeout' }}
      />
    );
    expect(screen.getByText('读取失败').getAttribute('title')).toBe('timeout');
  });

  test('部分来源失败时保留路由并提示数据不完整', () => {
    render(
      <ReverseProxyRoutes
        svc={{
          name: 'proxy',
          machine: 'aly',
          proxy_routes: [route],
          proxy_error: 'host: timeout',
        }}
      />
    );
    expect(screen.getByText(':3000 /static/*, /assets/*')).toBeTruthy();
    expect(screen.getByText('部分读取失败').getAttribute('title')).toBe('host: timeout');
  });
});
