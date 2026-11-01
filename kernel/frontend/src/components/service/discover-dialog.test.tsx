import { beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { DiscoverDialog } from './discover-dialog';
import { discoverMachineServices, registerDiscoveredServices } from '@/lib/api';
import type { DiscoveredContainer } from '@/lib/api';

vi.mock('@/lib/api', () => ({
  discoverMachineServices: vi.fn(),
  registerDiscoveredServices: vi.fn(),
  apiErrorMessage: vi.fn(() => '出错了'),
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const mockedDiscover = vi.mocked(discoverMachineServices);
const mockedRegister = vi.mocked(registerDiscoveredServices);

const container = (name: string, stack?: string): DiscoveredContainer => ({
  name,
  image: 'ghcr.io/x',
  status: 'Up 2 days',
  stack,
});

function renderDialog() {
  return render(
    <DiscoverDialog
      open
      machineId="nas"
      machineLabel="NAS"
      onOpenChange={vi.fn()}
      onAdded={vi.fn()}
    />
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedRegister.mockResolvedValue({ added: [], failed: [] });
});

describe('L4 发现箱按 compose 栈分组', () => {
  test('同一 project 的容器归一组，未分组的垫底', async () => {
    mockedDiscover.mockResolvedValue({
      ok: true,
      candidates: [
        container('beszel-agent'),
        container('ignis-proxy', 'ignis-stack'),
        container('ignis-web', 'ignis-stack'),
      ],
    });
    renderDialog();
    await waitFor(() => expect(screen.getByText('ignis-web')).toBeTruthy());

    const labels = screen
      .getAllByTestId('stack-group-label')
      .map((el) => el.textContent ?? '');
    expect(labels).toHaveLength(2);
    expect(labels[0]).toContain('ignis-stack');
    // 未分组永远排最后：手工起的容器不该顶在真实 stack 前面
    expect(labels[1]).toContain('未分组');
  });

  test('「选中整组」一次勾上整个 stack，再点一次取消', async () => {
    mockedDiscover.mockResolvedValue({
      ok: true,
      candidates: [container('ignis-web', 'ignis-stack'), container('ignis-proxy', 'ignis-stack')],
    });
    renderDialog();
    await waitFor(() => expect(screen.getByRole('button', { name: '选中整组' })).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: '选中整组' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /登记所选（2）/ })).toBeTruthy()
    );
    fireEvent.click(screen.getByRole('button', { name: '取消整组' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /登记所选（0）/ })).toBeTruthy()
    );
  });

  test('单容器的组不给「选中整组」（一个勾就够了）', async () => {
    mockedDiscover.mockResolvedValue({
      ok: true,
      candidates: [container('solo', 'solo-stack')],
    });
    renderDialog();
    await waitFor(() => expect(screen.getByText('solo')).toBeTruthy());
    expect(screen.queryByRole('button', { name: '选中整组' })).toBeNull();
  });

  test('分组只影响展示，登记 payload 里不带 stack（不写进台账）', async () => {
    mockedDiscover.mockResolvedValue({
      ok: true,
      candidates: [container('ignis-web', 'ignis-stack')],
    });
    mockedRegister.mockResolvedValue({ added: ['ignis-web'], failed: [] });
    renderDialog();
    await waitFor(() => expect(screen.getByText('ignis-web')).toBeTruthy());

    fireEvent.click(screen.getByLabelText('登记 ignis-web'));
    fireEvent.click(screen.getByRole('button', { name: /登记所选/ }));
    await waitFor(() => expect(mockedRegister).toHaveBeenCalled());
    const [, items] = mockedRegister.mock.calls[0];
    expect(items).toEqual([{ container: 'ignis-web', name: 'ignis-web', port: null }]);
  });

  test('后端没带 stack 字段时全部归「未分组」，不报错', async () => {
    mockedDiscover.mockResolvedValue({
      ok: true,
      candidates: [container('a'), container('b')],
    });
    renderDialog();
    await waitFor(() => expect(screen.getByText('a')).toBeTruthy());
    const group = screen.getByTestId('stack-group-label').parentElement?.parentElement;
    expect(group).toBeTruthy();
    expect(within(group as HTMLElement).getByText('b')).toBeTruthy();
  });
});
