import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, test } from 'vitest';
import { NotFoundRoute } from './not-found';

describe('NotFoundRoute', () => {
  test('显示 404 提示与回首页链接', () => {
    render(
      <MemoryRouter>
        <NotFoundRoute />
      </MemoryRouter>,
    );
    expect(screen.getByText('404')).toBeTruthy();
    expect((screen.getByRole('link', { name: '回到总控台' }) as HTMLAnchorElement).getAttribute('href')).toBe('/');
  });
});
