import { expect, test } from 'vitest';
import { serviceNavigation } from '@/lib/plugins/navigation-types';
import { auditNavigation } from './navigation';

test('审计服务深链携带精确 host/svc，跨机器不共用同一展开结果', () => {
  const group = { ...auditNavigation(), owner: 'audit' };
  const a = serviceNavigation([group], { name: 'axonhub', machine: 'txy' })[0].links[0];
  const b = serviceNavigation([group], { name: 'axonhub', machine: 'us' })[0].links[0];
  expect(a.to).toContain('host=txy&svc=axonhub');
  expect(b.to).toContain('host=us&svc=axonhub');
  expect(a.to).not.toEqual(b.to);
});
