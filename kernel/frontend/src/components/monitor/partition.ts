import type { Machine, MonitorContainer, MonitorSystem, Service } from '@/lib/types';

/**
 * §4.5 分区口径（B2 后两区都在总控台，设备页按机器 tab 走单机视图）：
 * - 服务器区：monitor.systems 中 machine 对应 role=server 或未知。
 * - 节点区：/api/machines 中 role=node。
 * - 两区都渲染 MachineCard（I0-2 后没有 SystemCard / NodeMachineCard 两层壳了）。
 * - role 未知一律按 server（向后兼容），kr/sg（role=node）永不计入服务器区。
 */

/** 由 /api/machines 结果构建 id → role 映射。 */
export function buildRoleMap(machines: Machine[]): Record<string, string> {
  const map: Record<string, string> = {};
  for (const m of machines) map[m.id] = m.role;
  return map;
}

/** 解析某个机器名的角色；未知（含 B 未就绪、machine 为空）一律按 server。 */
export function machineRole(machine: string | undefined, roleMap: Record<string, string>): string {
  if (machine && roleMap[machine]) return roleMap[machine];
  return 'server';
}

/** 服务器区：排除 role=node 的系统（kr/sg 不进服务器区）。 */
export function filterServerSystems(
  systems: MonitorSystem[],
  roleMap: Record<string, string>
): MonitorSystem[] {
  return systems.filter((sys) => machineRole(sys.machine, roleMap) !== 'node');
}

/** 节点区：仅 role=node 的机器。 */
export function filterNodeMachines(machines: Machine[]): Machine[] {
  return machines.filter((m) => m.role === 'node');
}

/**
 * §4.4 offlineServers 口径：role=server（未知按 server）且 status !== 'up' 的系统数。
 * kr/sg（role=node）永不计入。总控台与设备页共用同一实现，防两处口径漂移。
 */
export function countOfflineServers(
  systems: MonitorSystem[],
  roleMap: Record<string, string>
): number {
  return systems.filter(
    (sys) => machineRole(sys.machine, roleMap) === 'server' && sys.status !== 'up'
  ).length;
}

// ---- B2 页面架构（§5.2 / §6 B2）：单机过滤与机器 tab 计数 ----

/** 机器标识归一化（Beszel 系统名大写、台账 id 小写，统一小写比）。 */
export function machineIdOf(name?: string): string {
  return (name || '').toLowerCase();
}

/** 本机已登记服务（单机视图服务表 / tab 计数共用口径）。 */
export function filterServicesByMachine(services: Service[], machineId: string): Service[] {
  const target = machineIdOf(machineId);
  return services.filter((svc) => machineIdOf(svc.machine) === target);
}

/** 本机容器（Beszel 的 machine / system_name 归一化后与台账 id 对齐）。 */
export function filterContainersByMachine(
  containers: MonitorContainer[],
  machineId: string
): MonitorContainer[] {
  const target = machineIdOf(machineId);
  return containers.filter((c) => machineIdOf(c.machine || c.system_name) === target);
}

/**
 * 每台机器的已登记服务计数（机器 tab 条带计数用）。
 * node（kr/sg）没有 Docker 服务，计数为 0，由调用方决定显示 0 或「—」。
 */
export function countServicesByMachine(services: Service[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const svc of services) {
    const m = machineIdOf(svc.machine);
    if (m) counts[m] = (counts[m] || 0) + 1;
  }
  return counts;
}
