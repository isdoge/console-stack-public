import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, test } from 'vitest';
import { NavigationLink } from './navigation-link';

test('通用内部入口保留路由，外链使用独立浏览上下文', () => {
  render(<MemoryRouter><NavigationLink target={{ to: '/sample/one' }}>内部工具</NavigationLink>
    <NavigationLink target={{ to: 'https://example.test', external: true }}>外部工具</NavigationLink></MemoryRouter>);
  expect(screen.getByRole('link', { name: '内部工具' }).getAttribute('href')).toBe('/sample/one');
  const external = screen.getByRole('link', { name: '外部工具' });
  expect(external.getAttribute('href')).toBe('https://example.test');
  expect(external.getAttribute('target')).toBe('_blank');
  expect(external.getAttribute('rel')).toBe('noopener noreferrer');
});
