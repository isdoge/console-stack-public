import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { usePluginNavigation } from './navigation';
import { serviceNavigation, type PluginNavigation } from './navigation-types';

const fixture = vi.hoisted(() => ({
  load: vi.fn(), other: vi.fn(), states: [] as Array<{ id: string; enabled: boolean; visible: boolean; available?: boolean }>,
}));
vi.mock('./registry', () => ({
  pluginIsAvailable: (state: { available?: boolean }) => state?.available !== false,
  plugins: [
    { id: 'sample', navigation: true, loadNavigation: fixture.load },
    { id: 'other', navigation: true, loadNavigation: fixture.other },
  ],
}));
vi.mock('./provider', () => ({ usePlugins: () => ({ states: fixture.states }) }));

const group: PluginNavigation = {
  label: '工具入口', commandGroup: '工具', emptyMessage: '无匹配入口',
  links: [{ id: 'one', label: '工具', to: '/sample/one', machine: 'host', serviceNames: ['worker'] }],
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => {
  vi.resetAllMocks();
  fixture.states = [{ id: 'sample', enabled: true, visible: true }];
  fixture.load.mockResolvedValue(group);
  fixture.other.mockResolvedValue({ ...group, links: [] });
});

test('无安装状态或入口未打开时不调用插件加载器', async () => {
  fixture.states = [];
  const { result, rerender } = renderHook(({ active }) => usePluginNavigation(active), { initialProps: { active: true } });
  expect(result.current).toEqual([]);
  expect(fixture.load).not.toHaveBeenCalled();
  fixture.states = [{ id: 'sample', enabled: true, visible: true }];
  rerender({ active: false });
  expect(fixture.load).not.toHaveBeenCalled();
  rerender({ active: true });
  await waitFor(() => expect(result.current[0]?.owner).toBe('sample'));
  expect(fixture.load).toHaveBeenCalledOnce();
});

test.each(['disabled', 'hidden', 'unavailable'] as const)('%s立即撤销入口，迟到响应不能重新发布', async (state) => {
  const pending = deferred<PluginNavigation>();
  fixture.load.mockReturnValueOnce(pending.promise);
  const { result, rerender } = renderHook(() => usePluginNavigation());
  await waitFor(() => expect(fixture.load).toHaveBeenCalledOnce());
  fixture.states = [{ id: 'sample', enabled: state !== 'disabled', visible: state !== 'hidden', available: state !== 'unavailable' }];
  rerender();
  expect(result.current).toEqual([]);
  await act(async () => pending.resolve(group));
  expect(result.current).toEqual([]);
  fixture.states = [{ id: 'sample', enabled: true, visible: true }];
  rerender();
  await waitFor(() => expect(result.current[0]?.links).toEqual(group.links));
  expect(fixture.load).toHaveBeenCalledTimes(2);
});

test('关闭再打开必须重新读取，单个加载失败不拖垮其它贡献', async () => {
  const { result, rerender } = renderHook(({ active }) => usePluginNavigation(active), { initialProps: { active: true } });
  await waitFor(() => expect(result.current).toHaveLength(1));
  rerender({ active: false });
  expect(result.current).toEqual([]);
  fixture.load.mockRejectedValueOnce(new Error('offline'));
  fixture.states = [...fixture.states, { id: 'other', enabled: true, visible: true }];
  rerender({ active: true });
  await waitFor(() => expect(result.current.map((item) => item.owner)).toEqual(['other']));
});

test('服务关联必须同设备且精确命中名称或容器名', () => {
  const groups = [{ ...group, owner: 'sample', links: [
    ...group.links,
    { ...group.links[0], id: 'other-host', machine: 'another' },
    { ...group.links[0], id: 'substring', serviceNames: ['worker-extended'] },
  ] }];
  expect(serviceNavigation(groups, { name: 'worker', machine: 'host' })[0].links.map((link) => link.id)).toEqual(['one']);
  expect(serviceNavigation(groups, { name: 'api', container: 'worker', machine: 'host' })[0].links.map((link) => link.id)).toEqual(['one']);
});
