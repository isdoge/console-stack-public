import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Sidebar } from './sidebar';
import * as api from '@/lib/api';

// A3：sidebar.tsx 对 SIDEBAR_KEY 的读写目前若为裸 localStorage 调用，
// 隐私模式/配额异常会让 getItem/setItem 抛异常。读路径在 useState 惰性初始化里抛出
// 会让整个组件首渲染失败；写路径在 useEffect 里抛出会让交互后的 commit 阶段报错。
// 目标：两者都要降级为「用内存默认值继续渲染/交互」，不炸组件。

vi.mock('@/lib/plugins/registry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/plugins/registry')>()), plugins: [],
}));

vi.mock('@/lib/api', () => ({
  listMachines: vi.fn(),
}));

const mockedListMachines = vi.mocked(api.listMachines);

function renderSidebar() {
  return render(
    <MemoryRouter>
      <Sidebar />
    </MemoryRouter>
  );
}

describe('Sidebar 折叠态存储异常降级（A3 safe-browser-storage）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedListMachines.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  test('localStorage.getItem 抛异常时侧栏仍能渲染，折叠态兜底为展开', () => {
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    renderSidebar();
    // 只有 collapsed === false 时才渲染这段标题文字
    expect(screen.getByText('Console')).toBeTruthy();
  });

  test('localStorage.setItem 抛异常时点击收起/展开按钮仍能完成一次交互', () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    renderSidebar();
    const toggle = screen.getByLabelText('收起侧栏');
    fireEvent.click(toggle);
    expect(screen.getByLabelText('展开侧栏')).toBeTruthy();
  });
});
