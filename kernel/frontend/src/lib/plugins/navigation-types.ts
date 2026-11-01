import type { Service } from '@/lib/types';

/** 插件只向宿主投影导航所需字段，不传业务正文或凭据。 */
export interface NavigationTarget {
  id: string;
  label: string;
  to: string;
  external?: boolean;
  machine?: string;
  /** 同设备服务的精确名称或容器名；别名解析由插件完成。 */
  serviceNames?: string[];
}

/** 服务上下文深链：由宿主按当前服务展开 {name}/{machine}，不进命令面板。 */
export interface ServiceNavigationLink {
  id: string;
  label: string;
  to: string;
}

export interface PluginNavigation {
  label: string;
  commandGroup: string;
  emptyMessage: string;
  links: NavigationTarget[];
  /** 可选：服务抽屉/上下文的插件深链，缺席插件时宿主不渲染对应入口。 */
  serviceLinks?: ServiceNavigationLink[];
}

export interface NavigationContribution extends PluginNavigation {
  owner: string;
}

function expandServiceLink(to: string, service: Service) {
  return to
    .split('{name}').join(encodeURIComponent(service.name))
    .split('{machine}').join(encodeURIComponent(service.machine));
}

export function serviceNavigation(groups: NavigationContribution[], service: Service) {
  const names = new Set([service.name, service.container].filter(Boolean).map((name) => name!.toLowerCase()));
  return groups.map((group) => {
    const matched = group.links.filter((link) => link.machine === service.machine &&
      (link.serviceNames ?? [link.label]).some((name) => names.has(name.toLowerCase())));
    const serviceLinks = (group.serviceLinks ?? []).map((link) => ({
      id: link.id,
      label: link.label,
      to: expandServiceLink(link.to, service),
      machine: service.machine,
      serviceNames: [service.name],
    }));
    return { ...group, links: [...matched, ...serviceLinks] };
  });
}
