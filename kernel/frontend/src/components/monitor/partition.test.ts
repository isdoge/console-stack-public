import { describe, expect, it } from 'vitest';
import {
  buildRoleMap,
  countOfflineServers,
  countServicesByMachine,
  filterContainersByMachine,
  filterNodeMachines,
  filterServerSystems,
  filterServicesByMachine,
  machineIdOf,
  machineRole,
} from './partition';
import type { Machine, MonitorContainer, MonitorSystem, Service } from '@/lib/types';

const MACHINES: Machine[] = [
  { id: 'nas', label: 'NAS', role: 'server' },
  { id: 'txy', label: 'TXY', role: 'server' },
  { id: 'aly', label: 'ALY', role: 'server' },
  { id: 'us', label: 'Oracle', role: 'server' },
  { id: 'kr', label: '光帆 KR', role: 'node' },
  { id: 'sg', label: '光帆 SG', role: 'node' },
];

const SYSTEMS: MonitorSystem[] = [
  { id: '1', name: 'NAS', machine: 'nas' },
  { id: '2', name: 'TXY', machine: 'txy' },
  { id: '3', name: 'ALY', machine: 'aly' },
  { id: '4', name: 'US', machine: 'us' },
  { id: '5', name: 'KR', machine: 'kr' },
  { id: '6', name: 'SG', machine: 'sg' },
];

describe('设备页分区（§4.5）', () => {
  it('服务器区 4 台、节点区 2 台', () => {
    const roleMap = buildRoleMap(MACHINES);
    expect(filterServerSystems(SYSTEMS, roleMap).map((s) => s.name)).toEqual([
      'NAS',
      'TXY',
      'ALY',
      'US',
    ]);
    expect(filterNodeMachines(MACHINES).map((m) => m.id)).toEqual(['kr', 'sg']);
  });

  it('role=node 的 kr/sg 不进服务器区', () => {
    const roleMap = buildRoleMap(MACHINES);
    const servers = filterServerSystems(SYSTEMS, roleMap).map((s) => s.name);
    expect(servers).not.toContain('KR');
    expect(servers).not.toContain('SG');
  });

  it('role 未知一律按 server（含 B 未就绪时 roleMap 为空）', () => {
    const roleMap = buildRoleMap(MACHINES);
    expect(machineRole('unknown-host', roleMap)).toBe('server');
    expect(machineRole(undefined, roleMap)).toBe('server');
    // B 未就绪：无任何 machines 数据时，kr 也按 server 兜底（向后兼容）。
    expect(machineRole('kr', {})).toBe('server');
    expect(filterServerSystems(SYSTEMS, {})).toHaveLength(6);
  });
});

describe('countOfflineServers（§4.4 口径）', () => {
  const roleMap = buildRoleMap(MACHINES);
  const withStatus = (statuses: Record<string, string>) =>
    SYSTEMS.map((s) => ({ ...s, status: statuses[s.machine!] ?? 'up' }));

  it('只数 role=server 且 status!==up 的系统', () => {
    expect(countOfflineServers(withStatus({ aly: 'down' }), roleMap)).toBe(1);
    expect(countOfflineServers(withStatus({ aly: 'down', us: 'paused' }), roleMap)).toBe(2);
  });

  it('node（kr/sg）离线永不计入', () => {
    expect(countOfflineServers(withStatus({ kr: 'down', sg: 'down' }), roleMap)).toBe(0);
  });

  it('role 未知按 server 计入（roleMap 为空即 B 未就绪）', () => {
    expect(countOfflineServers(withStatus({ kr: 'down' }), {})).toBe(1);
  });

  it('全部 up 时为 0', () => {
    expect(countOfflineServers(withStatus({}), roleMap)).toBe(0);
  });
});

describe('单机过滤与机器 tab 计数（§5.2 / §6 B2）', () => {
  const SERVICES: Service[] = [
    { name: 'console-brain', machine: 'nas' },
    { name: 'axonhub', machine: 'nas' },
    { name: 'mihomo', machine: 'txy' },
    { name: 'edge-kr', machine: 'kr' },
  ];
  const CONTAINERS: MonitorContainer[] = [
    { system_id: '1', system_name: 'NAS', name: 'unregistered-a' },
    { system_id: '2', system_name: 'TXY', name: 'unregistered-b' },
    { system_id: '5', system_name: 'KR', name: 'unregistered-c' },
    { system_id: '9', machine: 'nas', name: 'registered' },
  ];

  it('machineIdOf 统一小写（Beszel 大写系统名 vs 台账小写 id）', () => {
    expect(machineIdOf('NAS')).toBe('nas');
    expect(machineIdOf('txy')).toBe('txy');
    expect(machineIdOf(undefined)).toBe('');
  });

  it('filterServicesByMachine 只留本机服务', () => {
    expect(filterServicesByMachine(SERVICES, 'nas').map((s) => s.name)).toEqual([
      'console-brain',
      'axonhub',
    ]);
    expect(filterServicesByMachine(SERVICES, 'kr').map((s) => s.name)).toEqual(['edge-kr']);
    expect(filterServicesByMachine(SERVICES, 'sg')).toEqual([]);
  });

  it('filterContainersByMachine 兼容 Beszel 大写 system_name 与归一化 machine', () => {
    expect(filterContainersByMachine(CONTAINERS, 'nas').map((c) => c.name)).toEqual([
      'unregistered-a',
      'registered',
    ]);
    expect(filterContainersByMachine(CONTAINERS, 'kr').map((c) => c.name)).toEqual([
      'unregistered-c',
    ]);
    expect(filterContainersByMachine(CONTAINERS, 'sg')).toEqual([]);
  });

  it('countServicesByMachine 按台账 id 计数', () => {
    expect(countServicesByMachine(SERVICES)).toEqual({ nas: 2, txy: 1, kr: 1 });
  });

  it('空 machine 不参与计数', () => {
    expect(countServicesByMachine([{ name: 'orphan', machine: '' }])).toEqual({});
  });
});
