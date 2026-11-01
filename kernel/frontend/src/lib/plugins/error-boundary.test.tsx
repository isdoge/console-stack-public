import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, test, vi } from 'vitest';
import { PluginErrorBoundary } from './error-boundary';

function BrokenPlugin(): ReactElement { throw new Error('private exception detail'); }

describe('PluginErrorBoundary', () => {
  test('contains a plugin render failure, hides exception details, and retries locally', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const HealthyFrameworkNavigation = () => <nav><a href="/devices">设备</a><a href="/settings">设置</a></nav>;
    const view = render(<><HealthyFrameworkNavigation /><PluginErrorBoundary pluginId="bad" pluginName="坏插件" kind="侧栏"><BrokenPlugin /></PluginErrorBoundary></>);
    expect(screen.getByRole('link', { name: '设备' })).toBeTruthy();
    expect(screen.getByRole('link', { name: '设置' })).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('坏插件的侧栏暂时无法显示');
    expect(document.body.textContent).not.toContain('private exception detail');
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(screen.getByRole('link', { name: '设备' })).toBeTruthy();
    error.mockRestore();
    view.unmount();
  });

  test('a failed plugin settings panel leaves neighboring framework settings available', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<main><section><h2>常规设置</h2><button>保存常规设置</button></section><section><h2>插件设置</h2><PluginErrorBoundary pluginId="bad" pluginName="坏插件" kind="设置"><BrokenPlugin /></PluginErrorBoundary></section><section><h2>安全清单</h2></section></main>);
    expect(screen.getByRole('heading', { name: '常规设置' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '保存常规设置' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '安全清单' })).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('坏插件的设置暂时无法显示');
    expect(document.body.textContent).not.toContain('private exception detail');
    error.mockRestore();
  });
});
