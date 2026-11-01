import { afterEach, describe, expect, test, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Topbar } from './topbar';
import { setDashboardEditController } from '@/components/dashboard/edit-mode';
import {
  clearInspectionActionsController,
  setInspectionActionsController,
} from '@/components/dashboard/inspection-actions';
import type { DashboardEditController } from '@/components/dashboard/edit-mode';
import type { InspectionActionsController } from '@/components/dashboard/inspection-actions';

function controllerOf(editing: boolean): DashboardEditController {
  return {
    editing,
    enter: vi.fn(),
    save: vi.fn(),
    cancel: vi.fn(),
    saving: false,
    reset: vi.fn(),
    hidden: [],
    show: vi.fn(),
  };
}

function renderTopbar(props: Partial<Parameters<typeof Topbar>[0]> = {}) {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Topbar {...props} />
    </MemoryRouter>
  );
}

function refreshButton() {
  return screen.getByRole('button', { name: /刷新/ }) as HTMLButtonElement;
}

afterEach(() => {
  setDashboardEditController(null);
  setInspectionActionsController(null);
});

describe('Topbar 巡检报告动作（C1，D4-2.3 收进顶栏）', () => {
  const controller: InspectionActionsController = {
    busy: null,
    onRun: vi.fn(),
  };

  test('总控台未挂载（未登记控制器）时不出动作按钮——其它页面顶栏保持原样', () => {
    renderTopbar({ onRefresh: vi.fn() });
    expect(screen.queryByRole('button', { name: '复制巡检报告' })).toBeNull();
    expect(screen.queryByRole('button', { name: '下载巡检报告' })).toBeNull();
  });

  test('登记控制器后两个动作出现且可点，busy 时双双禁用', () => {
    renderTopbar({ onRefresh: vi.fn() });
    act(() => setInspectionActionsController(controller));

    const copy = screen.getByRole('button', { name: '复制巡检报告' }) as HTMLButtonElement;
    const download = screen.getByRole('button', { name: '下载巡检报告' }) as HTMLButtonElement;
    expect(copy.disabled).toBe(false);
    expect(download.disabled).toBe(false);

    fireEvent.click(copy);
    expect(controller.onRun).toHaveBeenCalledWith('copy');
    fireEvent.click(download);
    expect(controller.onRun).toHaveBeenCalledWith('download');

    act(() => setInspectionActionsController({ ...controller, busy: 'copy' }));
    expect((screen.getByRole('button', { name: '复制巡检报告' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '下载巡检报告' }) as HTMLButtonElement).disabled).toBe(true);
  });

  test('卸载总控台（clear 只撤销自己那份）后动作消失', () => {
    renderTopbar({ onRefresh: vi.fn() });
    act(() => setInspectionActionsController(controller));
    expect(screen.getByRole('button', { name: '复制巡检报告' })).toBeTruthy();

    act(() => clearInspectionActionsController(controller));
    expect(screen.queryByRole('button', { name: '复制巡检报告' })).toBeNull();
  });
});

describe('Topbar 刷新按钮 × 布局编辑态（C8/D3）', () => {
  test('没进编辑态时刷新照常可点', () => {
    const onRefresh = vi.fn();
    renderTopbar({ onRefresh });

    expect(refreshButton().disabled).toBe(false);
    fireEvent.click(refreshButton());
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  test('编辑态期间刷新禁用——refreshKey 一变卡片整片重渲染会打断拖拽手势', () => {
    const onRefresh = vi.fn();
    renderTopbar({ onRefresh });

    act(() => setDashboardEditController(controllerOf(true)));

    expect(refreshButton().disabled).toBe(true);
    expect(refreshButton().title).toBe('布局编辑中已暂停刷新');
    fireEvent.click(refreshButton());
    expect(onRefresh).not.toHaveBeenCalled();
  });

  test('退出编辑态后刷新恢复', () => {
    const onRefresh = vi.fn();
    renderTopbar({ onRefresh });

    act(() => setDashboardEditController(controllerOf(true)));
    expect(refreshButton().disabled).toBe(true);

    act(() => setDashboardEditController(controllerOf(false)));
    expect(refreshButton().disabled).toBe(false);
    fireEvent.click(refreshButton());
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  test('refreshing 与编辑态各自独立禁用，互不掩盖', () => {
    renderTopbar({ onRefresh: vi.fn(), refreshing: true });
    expect(refreshButton().disabled).toBe(true);
    // refreshing 态不该借用编辑态的提示文案
    expect(refreshButton().title).toBe('');
  });

  test('总控台没登记控制器时顶栏不出编辑入口（其它页面 / 窄屏）', () => {
    renderTopbar({ onRefresh: vi.fn() });
    expect(screen.queryByRole('button', { name: '编辑布局' })).toBeNull();
  });

  test('登记控制器后铅笔按钮出现在刷新按钮之前', () => {
    renderTopbar({ onRefresh: vi.fn() });
    act(() => setDashboardEditController(controllerOf(false)));

    const pencil = screen.getByRole('button', { name: '编辑布局' });
    expect(pencil).toBeTruthy();
    expect(
      pencil.compareDocumentPosition(refreshButton()) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });
});
