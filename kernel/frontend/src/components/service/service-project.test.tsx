import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, test, vi } from 'vitest';
import { DeviceServiceContext } from '@/components/devices/device-service-context';
import { ServiceContext } from './service-context';
import { serviceProject } from '@/lib/service-context';
import type { Service } from '@/lib/types';

vi.mock('@/lib/plugins/provider', () => ({ usePluginVisible: () => false }));

const service: Service = {
  name: 'sample-service', machine: 'sample-host', actions: [],
  project: { repo: 'https://example.test/project', links: [
    { label: '运维资料', href: 'https://example.test/project/runbook' },
    { label: '不可打开', href: 'javascript:alert(1)' },
    { label: '凭据地址', href: 'https://user:password@example.test/private' },
  ] },
};

test.each(['compact', 'device'] as const)('%s服务上下文使用台账项目资料，只展示安全链接', (kind) => {
  render(<MemoryRouter>{kind === 'compact'
    ? <ServiceContext service={service} onClose={vi.fn()} onLogs={vi.fn()} />
    : <DeviceServiceContext service={service} mode="inline" onClose={vi.fn()} />
  }</MemoryRouter>);
  expect(screen.getByRole('link', { name: '项目仓库' }).getAttribute('href')).toBe('https://example.test/project');
  const link = screen.getByRole('link', { name: '运维资料' });
  expect(link.getAttribute('href')).toBe('https://example.test/project/runbook');
  expect(link.getAttribute('target')).toBe('_blank');
  expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  expect(screen.queryByRole('link', { name: '不可打开' })).toBeNull();
  expect(screen.queryByRole('link', { name: '凭据地址' })).toBeNull();
});

test('缺少或损坏的项目资料不猜测地址，合法资料链接可独立于仓库', () => {
  expect(serviceProject({ name: 'sample-service', machine: 'sample-host' })).toBeNull();
  expect(serviceProject({ ...service, project: JSON.parse('{"repo":17,"links":[null,{},false]}') })).toBeNull();
  expect(serviceProject({ ...service, project: { links: [
    { label: '  资料  ', href: 'https://example.test/docs' },
  ] } })).toEqual({ repo: null, links: [{ label: '资料', href: 'https://example.test/docs' }] });
});
