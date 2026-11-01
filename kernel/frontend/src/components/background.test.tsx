import { describe, expect, test } from 'vitest';
import { render } from '@testing-library/react';
import { Background } from './background';

describe('Background（D3 背景层）', () => {
  test('渲染 fixed 全屏、无交互、渐变 mesh 走 .bg-mesh（亮/暗变体在 index.css）', () => {
    const { container } = render(<Background />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).not.toBeNull();
    expect(el.className).toContain('fixed');
    expect(el.className).toContain('inset-0');
    expect(el.className).toContain('-z-10');
    expect(el.className).toContain('pointer-events-none');
    expect(el.className).toContain('bg-mesh');
    expect(el.getAttribute('aria-hidden')).toBe('true');
  });
});
