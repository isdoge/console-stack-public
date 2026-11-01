import { renderHook, act } from '@testing-library/react';
import { afterEach, describe, expect, test } from 'vitest';
import { THEME_KEY, useTheme } from './use-theme';

describe('useTheme（T12 暗色模式）', () => {
  afterEach(() => {
    localStorage.removeItem(THEME_KEY);
    document.documentElement.classList.remove('dark');
  });

  test('默认跟随系统：系统暗则挂 .dark class', () => {
    matchMediaMock(false);
    localStorage.setItem(THEME_KEY, 'system');
    const { result } = renderHook(() => useTheme());
    expect(result.current.pref).toBe('system');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  test('显式 dark：重新实例化读回偏好并落 class', () => {
    matchMediaMock(false);
    localStorage.setItem(THEME_KEY, 'dark');
    const { result } = renderHook(() => useTheme());
    expect(result.current.pref).toBe('dark');
    expect(result.current.resolved).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  test('toggle 在亮暗之间往返并写回 localStorage', () => {
    matchMediaMock(false);
    const { result } = renderHook(() => useTheme());
    expect(result.current.resolved).toBe('light');
    act(() => result.current.toggle());
    expect(result.current.resolved).toBe('dark');
    expect(localStorage.getItem(THEME_KEY)).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    act(() => result.current.toggle());
    expect(result.current.resolved).toBe('light');
    expect(localStorage.getItem(THEME_KEY)).toBe('light');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  test('非法存储值回落 system，不炸', () => {
    matchMediaMock(true);
    localStorage.setItem(THEME_KEY, 'blue');
    const { result } = renderHook(() => useTheme());
    expect(result.current.pref).toBe('system');
    expect(result.current.resolved).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });
});

/** jsdom 无 matchMedia：按需打桩。 */
function matchMediaMock(dark: boolean) {
  window.matchMedia = ((query: string) =>
    ({
      matches: dark && query.includes('dark'),
      addEventListener: () => {},
      removeEventListener: () => {},
    })) as unknown as typeof window.matchMedia;
}
