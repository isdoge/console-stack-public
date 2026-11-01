import { afterEach, describe, expect, test, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';
import {
  DashboardEditControls,
  EDIT_MIN_WIDTH,
  clearDashboardEditController,
  setDashboardEditController,
  useLayoutEditable,
} from './edit-mode';
import type { DashboardEditController } from './edit-mode';

function controllerOf(overrides: Partial<DashboardEditController> = {}): DashboardEditController {
  return {
    editing: false,
    enter: vi.fn(),
    save: vi.fn(),
    cancel: vi.fn(),
    saving: false,
    reset: vi.fn(),
    hidden: [],
    show: vi.fn(),
    ...overrides,
  };
}

function setViewport(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
}

afterEach(() => {
  setDashboardEditController(null);
  setViewport(1024); // jsdom 默认值，别把宽度漏给下一条用例
});

describe('DashboardEditControls（C8/D3）', () => {
  test('没有总控台登记控制器时整块不渲染——其它页面顶栏保持原样', () => {
    const { container } = render(<DashboardEditControls />);
    expect(container.innerHTML).toBe('');
  });

  test('查看态只有铅笔按钮，点它进编辑态', () => {
    const enter = vi.fn();
    act(() => setDashboardEditController(controllerOf({ enter })));
    render(<DashboardEditControls />);

    expect(screen.getByRole('button', { name: '编辑布局' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '添加 widget' })).toBeNull();
    expect(screen.queryByRole('button', { name: '重置默认布局' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '编辑布局' }));
    expect(enter).toHaveBeenCalledTimes(1);
  });

  test('编辑态：添加 widget / 重置 / 放弃 / 保存 四件，铅笔消失（D1\' 会话制：出口只有保存与放弃）', () => {
    const reset = vi.fn();
    const save = vi.fn();
    const cancel = vi.fn();
    act(() =>
      setDashboardEditController(controllerOf({ editing: true, reset, save, cancel }))
    );
    render(<DashboardEditControls />);

    expect(screen.getByRole('button', { name: '保存布局' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '放弃布局修改' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '编辑布局' })).toBeNull();
    expect(screen.getByRole('button', { name: '添加 widget' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '重置默认布局' }));
    expect(reset).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '保存布局' }));
    expect(save).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '放弃布局修改' }));
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  test('保存请求在途时保存 / 放弃都禁用，防重复提交', () => {
    act(() => setDashboardEditController(controllerOf({ editing: true, saving: true })));
    render(<DashboardEditControls />);

    expect((screen.getByRole('button', { name: '保存布局' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '放弃布局修改' }) as HTMLButtonElement).disabled).toBe(true);
  });

  test('「添加 widget」列出被隐藏的卡，选一项调 show', () => {
    const show = vi.fn();
    act(() =>
      setDashboardEditController(
        controllerOf({
          editing: true,
          hidden: [{ key: 'machine-card:nas', label: 'NAS' }],
          show,
        })
      )
    );
    render(<DashboardEditControls />);

    fireEvent.click(screen.getByRole('button', { name: '添加 widget' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'NAS' }));

    expect(show).toHaveBeenCalledWith('machine-card:nas');
    // 选完菜单收起
    expect(screen.queryByRole('menuitem', { name: 'NAS' })).toBeNull();
  });

  test('没有隐藏卡时「添加 widget」给出说明而不是空面板', () => {
    act(() => setDashboardEditController(controllerOf({ editing: true })));
    render(<DashboardEditControls />);

    fireEvent.click(screen.getByRole('button', { name: '添加 widget' }));
    expect(screen.getByText('没有被隐藏的卡片')).toBeTruthy();
  });

  test('控制器撤销后顶栏立刻清空（离开总控台）', () => {
    const controller = controllerOf();
    act(() => setDashboardEditController(controller));
    const { container } = render(<DashboardEditControls />);
    expect(container.innerHTML).not.toBe('');

    act(() => clearDashboardEditController(controller));
    expect(container.innerHTML).toBe('');
  });

  test('clearDashboardEditController 只撤销自己那份，不误清后来者', () => {
    const first = controllerOf();
    const second = controllerOf({ editing: true });
    act(() => setDashboardEditController(first));
    act(() => setDashboardEditController(second));
    render(<DashboardEditControls />);

    act(() => clearDashboardEditController(first)); // 迟到的旧 cleanup
    expect(screen.getByRole('button', { name: '保存布局' })).toBeTruthy();
  });
});

describe('useLayoutEditable（C8/D3 编辑态只在 12 列档开放）', () => {
  test(`≥${EDIT_MIN_WIDTH} 可编辑`, () => {
    setViewport(1440);
    const { result } = renderHook(() => useLayoutEditable());
    expect(result.current).toBe(true);
  });

  test(`<${EDIT_MIN_WIDTH}（6 列 / 1 列档）不可编辑`, () => {
    setViewport(1279);
    const { result } = renderHook(() => useLayoutEditable());
    expect(result.current).toBe(false);

    setViewport(600);
    const narrow = renderHook(() => useLayoutEditable());
    expect(narrow.result.current).toBe(false);
  });

  test('缩窗跨过 1280 会现场翻脸', () => {
    setViewport(1440);
    const { result } = renderHook(() => useLayoutEditable());
    expect(result.current).toBe(true);

    act(() => {
      setViewport(1000);
      window.dispatchEvent(new Event('resize'));
    });
    expect(result.current).toBe(false);

    act(() => {
      setViewport(1400);
      window.dispatchEvent(new Event('resize'));
    });
    expect(result.current).toBe(true);
  });
});
