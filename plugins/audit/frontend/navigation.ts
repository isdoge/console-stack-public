import type { PluginNavigation } from '@/lib/plugins/navigation-types';

export function auditNavigation(): PluginNavigation {
  return {
    label: '审计与事件',
    commandGroup: '审计',
    emptyMessage: '未找到与该服务相关的审计入口。',
    links: [{ id: 'audit-home', label: '审计与事件', to: '/audit' }],
    serviceLinks: [
      {
        id: 'audit-oplog',
        label: '操作记录',
        to: '/audit?tab=oplog&q={name}&host={machine}&svc={name}',
      },
    ],
  };
}

export async function loadNavigation(): Promise<PluginNavigation> {
  return auditNavigation();
}
