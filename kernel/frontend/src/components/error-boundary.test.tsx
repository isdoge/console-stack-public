import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, test, vi, afterEach } from 'vitest';
import { ErrorBoundary } from './error-boundary';

function Boom(): never {
  throw new Error('渲染爆炸');
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ErrorBoundary', () => {
  test('子组件正常时原样渲染', () => {
    render(
      <ErrorBoundary>
        <p>正常内容</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText('正常内容')).toBeTruthy();
  });

  test('子组件崩溃时显示兜底页与刷新按钮', () => {
    const silence = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText('页面出错了')).toBeTruthy();
    expect(screen.getByText(/渲染爆炸/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '刷新页面' }));
    expect(silence).toHaveBeenCalled();
  });
});
