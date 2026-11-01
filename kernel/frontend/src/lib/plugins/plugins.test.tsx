import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { PluginChromeProvider, PluginProvider, usePluginChrome, usePlugins } from './provider';
import { loadRequiredPluginExport, plugins, pluginForPath, resolveRegisteredPlugins } from './registry';
import { PluginManager } from './plugin-manager';
import { SidebarContent } from '@/components/layout/sidebar';

vi.mock('@/lib/api', () => ({ listMachines: vi.fn().mockResolvedValue([]) }));
let rows = plugins.map((p) => ({ ...p, enabled: true, visible: true }));
const requests: { url: string; method?: string }[] = [];
beforeEach(() => {
  rows = plugins.map((p) => ({ ...p, enabled: true, visible: true }));
  requests.length = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    requests.push({ url, method: init?.method });
    if (init?.method === 'PUT') {
      const id = url.split('/').pop();
      rows = rows.map((p) => p.id === id ? { ...p, ...JSON.parse(String(init.body)) } : p);
      return { ok: true, json: async () => rows.find((p) => p.id === id) };
    }
    return { ok: true, json: async () => rows };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function Probe() {
  const { states, error } = usePlugins();
  return <output>{JSON.stringify({ states: states.map(({ id, enabled, visible }) => ({ id, enabled, visible })), error })}</output>;
}

describe('plugin lifecycle UI', () => {
  test('manifest discovery and parameter paths share one registry', () => {
    expect(plugins.map((p) => p.id)).toEqual(['feed', 'ledger', 'renewals', 'audit', 'personal_workbench', 'backup_evidence', 'panels']);
    expect(pluginForPath('/panel/16')?.id).toBe('panels');
    expect(pluginForPath('/devices')).toBeUndefined();
  });
  test('registry filters by declared composition list and rejects missing declared plugins', () => {
    const mkManifest = (id: string, order = 10) => ({
      id,
      name: id,
      description: `${id} desc`,
      version: '1.0.0',
      api_version: 1,
      order,
      owns: { tables: [], settings: [], files: [], tasks: [] },
      routes: [`/${id}`],
      api_prefixes: [`/api/${id}`],
    });
    const manifests = {
      '/ext_root/listed/manifest.json': mkManifest('listed', 5),
      '/ext_root/unlisted/manifest.json': mkManifest('unlisted', 1),
    };
    const entries = {
      '/ext_root/listed/frontend/index.tsx': async () => ({ default: () => null }),
      '/ext_root/unlisted/frontend/index.tsx': async () => ({ default: () => null }),
    };
    const registered = resolveRegisteredPlugins(manifests, entries, { plugins: ['listed'] });
    expect(registered.map((p) => p.id)).toEqual(['listed']);
    expect(() => resolveRegisteredPlugins(manifests, entries, { plugins: ['listed', 'ghost'] })).toThrow(/不存在的插件.*ghost/);
    expect(() => resolveRegisteredPlugins(manifests, entries, { plugins: ['../bad'] })).toThrow(/无效插件 id/);
  });
  test('legacy status without available stays available, while runtime failure is shown distinctly', async () => {
    rows = plugins.map((p) => ({ ...p, enabled: true, visible: true, available: p.id !== 'feed', error: p.id === 'feed' ? '初始化失败' : null }));
    render(<PluginProvider><PluginManager /></PluginProvider>);
    await screen.findByText('初始化失败');
    expect(screen.getByText('运行故障')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重新启用并重试' }));
    await waitFor(() => expect(requests.some((r) => r.url === '/api/plugins/feed' && r.method === 'PUT')).toBe(true));
    expect(rows.find((row) => row.id === 'feed')?.enabled).toBe(true);
    expect(rows.find((row) => row.id === 'ledger')).toBeTruthy();
  });
  test('a missing declared export rejects instead of rendering an empty component', async () => {
    await expect(loadRequiredPluginExport('sample', 'Sidebar', async () => ({ default: () => null }), true)).rejects.toThrow(/声明了 Sidebar/);
    await expect(loadRequiredPluginExport('sample', 'Settings', async () => ({ default: () => null }), true)).rejects.toThrow(/声明了 Settings/);
  });
  test('toolbar registrations are isolated by owner and have stable registry order', async () => {
    function Register({ owner, remove }: { owner: string; remove?: boolean }) {
      const { registerToolbar } = usePluginChrome();
      useEffect(() => {
        if (!remove) registerToolbar(owner, <button>{owner}</button>);
        return () => registerToolbar(owner, null);
      }, [owner, remove, registerToolbar]);
      return null;
    }
    function ProbeToolbar() { return <>{usePluginChrome().toolbar?.node}</>; }
    const view = render(<PluginChromeProvider><Register owner="audit" /><Register owner="feed" /><ProbeToolbar /></PluginChromeProvider>);
    await waitFor(() => expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['feed', 'audit']));
    view.rerender(<PluginChromeProvider><Register owner="audit" /><ProbeToolbar /></PluginChromeProvider>);
    await waitFor(() => expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['audit']));
  });
  test('toolbar owners are removed as soon as authority marks them disabled or unavailable', async () => {
    function Register({ owner }: { owner: string }) {
      const { registerToolbar } = usePluginChrome();
      useEffect(() => {
        registerToolbar(owner, <button>{owner}</button>);
        return () => registerToolbar(owner, null);
      }, [owner, registerToolbar]);
      return null;
    }
    function ToolbarProbe() { return <>{usePluginChrome().toolbar?.node}</>; }
    function Reload() { const { reload } = usePlugins(); return <button onClick={() => void reload()}>重新读取状态</button>; }
    render(<PluginProvider><PluginChromeProvider><Register owner="audit" /><Register owner="feed" /><ToolbarProbe /><Reload /></PluginChromeProvider></PluginProvider>);
    await waitFor(() => expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['feed', 'audit', '重新读取状态']));
    rows = rows.map((row) => row.id === 'feed' ? { ...row, available: false, error: 'runtime error' } : row);
    fireEvent.click(screen.getByRole('button', { name: '重新读取状态' }));
    await waitFor(() => expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['audit', '重新读取状态']));
    rows = rows.map((row) => row.id === 'audit' ? { ...row, enabled: false } : row);
    fireEvent.click(screen.getByRole('button', { name: '重新读取状态' }));
    await waitFor(() => expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['重新读取状态']));
  });
  test('hide removes navigation while enabled state remains true; remount reads persisted authority', async () => {
    const view = render(<MemoryRouter><PluginProvider><PluginManager /><SidebarContent /><Probe /></PluginProvider></MemoryRouter>);
    await screen.findByRole('switch', { name: '显示个人记账入口' });
    expect(screen.getByRole('link', { name: '个人记账' })).toBeTruthy();
    fireEvent.click(screen.getByRole('switch', { name: '显示个人记账入口' }));
    await waitFor(() => expect(screen.queryByRole('link', { name: '个人记账' })).toBeNull());
    expect(rows.find((p) => p.id === 'ledger')).toMatchObject({ enabled: true, visible: false });
    view.unmount();
    render(<PluginProvider><Probe /></PluginProvider>);
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('"id":"ledger","enabled":true,"visible":false'));
  });
  test('disable changes authoritative state and retains visibility preference for reenabling', async () => {
    render(<PluginProvider><PluginManager /></PluginProvider>);
    fireEvent.click(await screen.findByRole('switch', { name: '启用信息聚合' }));
    await waitFor(() => expect((screen.getByRole('switch', { name: '启用信息聚合' }) as HTMLInputElement).checked).toBe(false));
    expect(rows.find((p) => p.id === 'feed')).toMatchObject({ enabled: false, visible: true });
    fireEvent.click(screen.getByRole('switch', { name: '启用信息聚合' }));
    await waitFor(() => expect((screen.getByRole('switch', { name: '启用信息聚合' }) as HTMLInputElement).checked).toBe(true));
  });
  test('failed save does not pretend to be disabled', async () => {
    render(<PluginProvider><PluginManager /></PluginProvider>);
    await screen.findByRole('switch', { name: '启用个人记账' });
    const fetcher = vi.mocked(fetch);
    fetcher.mockImplementationOnce(async () => ({ ok: false, json: async () => ({ error: '维护只读' }) }) as Response);
    fireEvent.click(screen.getByRole('switch', { name: '启用个人记账' }));
    await screen.findByText('维护只读');
    expect((screen.getByRole('switch', { name: '启用个人记账' }) as HTMLInputElement).checked).toBe(true);
  });
  test('state read failure fails closed and leaves manager retry available', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('offline'));
    render(<MemoryRouter><PluginProvider><PluginManager /><SidebarContent /></PluginProvider></MemoryRouter>);
    await screen.findByText('offline');
    expect(screen.queryByRole('link', { name: '个人记账' })).toBeNull();
    expect(screen.getByRole('link', { name: '设备' })).toBeTruthy();
  });
  test('late pre-save read cannot overwrite the confirmed update', async () => {
    render(<PluginProvider><PluginManager /></PluginProvider>);
    await screen.findByRole('switch', { name: '启用信息聚合' });
    let resolveRead: (value: Response) => void;
    const staleRows = rows.map((row) => ({ ...row }));
    vi.mocked(fetch).mockImplementationOnce(() => new Promise((resolve) => { resolveRead = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));
    fireEvent.click(screen.getByRole('switch', { name: '启用信息聚合' }));
    await waitFor(() => expect((screen.getByRole('switch', { name: '启用信息聚合' }) as HTMLInputElement).checked).toBe(false));
    await act(async () => { resolveRead!({ ok: true, json: async () => staleRows } as Response); });
    expect((screen.getByRole('switch', { name: '启用信息聚合' }) as HTMLInputElement).checked).toBe(false);
  });
});
