import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { SettingsRoute } from './settings';
import * as api from '@/lib/api';
import type { ConsoleSettings } from '@/lib/types';

vi.mock('@/lib/plugins/registry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/plugins/registry')>()), plugins: [],
}));
vi.mock('@/lib/api', () => ({
  getConsoleSettings: vi.fn(), saveConsoleSettings: vi.fn(),
  getEffectiveConfig: vi.fn(async () => ({ groups: {}, machines: [], schema_version: null, running_commit: null })),
  apiErrorMessage: (error: Error) => error.message,
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const settings: ConsoleSettings = {
  generated_at: 'synthetic', settings_file: 'settings.yaml', settings_file_exists: true,
  groups: { general: { title: '常规', entries: [{ key: 'SAMPLE_INTERVAL', title: '合成间隔',
    description: '测试字段', value: '600', source: 'default', writable: true, readonly_reason: null,
    type: 'int', min: 60, max: 86400, unit: '秒', default: '600', requires_restart: false }] } },
  tables: [], never_writable: [],
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getConsoleSettings).mockResolvedValue(settings);
  vi.mocked(api.saveConsoleSettings).mockImplementation(async (values) => ({ ...settings,
    groups: { general: { ...settings.groups.general, entries: [{ ...settings.groups.general.entries[0], value: values.SAMPLE_INTERVAL }] } },
    changed: ['SAMPLE_INTERVAL'], pruned_oplog: 0,
  }));
});
afterEach(() => window.history.replaceState(null, '', window.location.pathname));

test('无插件时框架页签可用，切换不卸载草稿，保存仅提交修改字段', async () => {
  render(<SettingsRoute refreshKey={0} />);
  const input = await screen.findByLabelText('合成间隔') as HTMLInputElement;
  expect(screen.getAllByRole('tab').map((tab) => tab.id)).toEqual([
    'settings-tab-general', 'settings-tab-notify', 'settings-tab-data',
    'settings-tab-plugins', 'settings-tab-security', 'settings-tab-about',
  ]);
  fireEvent.change(input, { target: { value: '900' } });
  fireEvent.click(screen.getByRole('tab', { name: '安全' }));
  fireEvent.click(screen.getByRole('tab', { name: /常规/ }));
  expect(input.value).toBe('900');
  fireEvent.click(within(screen.getByRole('region', { name: '常规' })).getByRole('button', { name: '保存（1）' }));
  await waitFor(() => expect(api.saveConsoleSettings).toHaveBeenCalledWith({ SAMPLE_INTERVAL: '900' }));
  expect(input.value).toBe('900');
});

test('无插件的保存仍隔离旧读取与未提交编辑', async () => {
  let resolveRead!: (value: ConsoleSettings) => void;
  const { rerender } = render(<SettingsRoute refreshKey={0} />);
  const input = await screen.findByLabelText('合成间隔') as HTMLInputElement;
  vi.mocked(api.getConsoleSettings).mockReturnValueOnce(new Promise((done) => { resolveRead = done; }));
  rerender(<SettingsRoute refreshKey={1} />);
  fireEvent.change(input, { target: { value: '900' } });
  fireEvent.click(screen.getByRole('button', { name: '保存（1）' }));
  await waitFor(() => expect(api.saveConsoleSettings).toHaveBeenCalledOnce());
  await act(async () => resolveRead(settings));
  expect(input.value).toBe('900');
});
