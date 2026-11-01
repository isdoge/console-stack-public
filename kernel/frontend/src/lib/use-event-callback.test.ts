import { renderHook } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { useEventCallback } from './use-event-callback';

describe('useEventCallback', () => {
  test('返回稳定引用，且始终调用最新实现', () => {
    const first = vi.fn((_x: string) => 'first' as string);
    const { result, rerender } = renderHook(({ fn }) => useEventCallback(fn), {
      initialProps: { fn: first },
    });
    const stable = result.current;
    expect(result.current('a')).toBe('first');

    const second = vi.fn((_x: string) => 'second' as string);
    rerender({ fn: second });
    expect(result.current).toBe(stable);
    expect(result.current('b')).toBe('second');
    expect(second).toHaveBeenCalledWith('b');
  });
});
