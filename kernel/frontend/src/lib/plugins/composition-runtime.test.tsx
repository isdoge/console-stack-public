import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, expect, test, vi } from 'vitest';
import { plugins } from './registry';
import { usePluginNavigation } from './navigation';
import { CommandPalette } from '@/components/command-palette';
import { DevicesRoute } from '@/routes/devices';
import { SettingsRoute } from '@/routes/settings';

const requests = vi.hoisted(() => [] as string[]);
vi.mock('axios', () => ({ default: { create: () => ({
  get: async (url: string) => { requests.push(url); return { data: [] }; },
}) } }));
vi.mock('@/lib/api', () => ({
  listMachines: vi.fn(async () => []), listServices: vi.fn(async () => []),
  getConfig: vi.fn(async () => ({ probe_interval: 600 })),
  getMonitor: vi.fn(async () => ({ systems: [], containers: [] })),
  getServicesSnapshot: vi.fn(async () => ({ services: [], snapshot: { sampledAt: null, refreshing: false, error: false } })),
  getCapacity: vi.fn(async () => ({ machines: {} })),
  getObservability: vi.fn(async () => ({ active_alerts: [], recent_events: [], systemd_services: [], smart_devices: [], machine_links: {} })),
  getConsoleSettings: vi.fn(async () => ({ groups: {}, tables: [], never_writable: [] })),
  getEffectiveConfig: vi.fn(async () => ({ groups: {}, machines: [], schema_version: null, running_commit: null })),
  apiErrorMessage: (error: Error) => error.message,
}));
beforeEach(() => { requests.length = 0; });

const declaresNavigation = plugins.some((plugin) => plugin.navigation);

test('真实组合决定是否加载私人导航接口', async () => {
  const { result } = renderHook(() => usePluginNavigation());
  if (declaresNavigation) await waitFor(() => expect(result.current.length).toBeGreaterThan(0));
  else await act(async () => {});
  expect(requests.includes('/panels')).toBe(declaresNavigation);
  if (!declaresNavigation) expect(result.current).toEqual([]);
});

test('命令、设备和设置宿主在缺席组合下不请求panels', async () => {
  render(<MemoryRouter><CommandPalette /><Routes>
    <Route path="*" element={<><DevicesRoute refreshKey={0} /><SettingsRoute refreshKey={0} /></>} />
  </Routes></MemoryRouter>);
  fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
  await screen.findByText('快速跳转');
  await waitFor(() => expect(screen.queryByText('正在加载设置…')).toBeNull());
  if (declaresNavigation) await waitFor(() => expect(requests.includes('/panels')).toBe(true));
  else {
    await act(async () => {});
    expect(requests.filter((url) => url.includes('panels'))).toEqual([]);
    expect(screen.queryByRole('tab', { name: '面板' })).toBeNull();
  }
});
