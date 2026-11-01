import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import {
  AttentionStrip,
  attentionDismissKey,
  localDateKey,
  readDismissed,
  writeDismissed,
} from './attention-strip';
import type { AttentionItem } from '@/lib/types';
import * as api from '@/lib/api';

vi.mock('@/lib/api', () => ({
  getAttention: vi.fn(),
}));

const mockedGet = vi.mocked(api.getAttention);

const sample: AttentionItem[] = [
  {
    severity: 'error',
    kind: 'probe',
    title: 'svc-a 健康检查失败',
    detail: 'connection refused',
    link: '/',
  },
  {
    severity: 'warn',
    kind: 'version',
    title: 'svc-b 有可更新版本',
    detail: '1.0 → 1.1',
    link: '/',
  },
];

describe('Storage getter 异常降级', () => {
  test.each(['read', 'write'] as const)('%s helper 不让 SecurityError 逃出', (operation) => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')!;
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() { throw new DOMException('denied', 'SecurityError'); },
    });
    try {
      if (operation === 'read') {
        expect(readDismissed()).toEqual(new Set());
      } else {
        expect(() => writeDismissed(new Set(['probe|test']))).not.toThrow();
      }
    } finally {
      Object.defineProperty(globalThis, 'localStorage', descriptor);
    }
  });

  test('getter 抛错时仍能渲染、单项忽略和全部已读', () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')!;
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() { throw new DOMException('denied', 'SecurityError'); },
    });
    try {
      renderStrip(sample);
      expect(screen.getByText('2 项')).toBeTruthy();
      fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
      expect(screen.queryByText(sample[0].title)).toBeNull();
      expect(screen.getByText(sample[1].title)).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: '全部已读' }));
      expect(screen.queryByText(sample[1].title)).toBeNull();
      expect(screen.getByText('无待办')).toBeTruthy();
    } finally {
      Object.defineProperty(globalThis, 'localStorage', descriptor);
    }
  });

  test('getItem 抛错时读取退化为空 Set', () => {
    const storage = { getItem: () => { throw new DOMException('denied', 'SecurityError'); } } as unknown as Storage;
    expect(readDismissed(storage)).toEqual(new Set());
  });

  test('序列化抛错时写入静默降级', () => {
    const storage = { setItem: vi.fn() } as unknown as Storage;
    const stringify = vi.spyOn(JSON, 'stringify').mockImplementation(() => {
      throw new Error('serialization failed');
    });
    try {
      expect(() => writeDismissed(new Set(['probe|test']), storage)).not.toThrow();
      expect(storage.setItem).not.toHaveBeenCalled();
    } finally {
      stringify.mockRestore();
    }
  });

  test('payload 构造抛错时写入静默降级', () => {
    const keys = new Set<string>();
    vi.spyOn(keys, Symbol.iterator).mockImplementation(() => {
      throw new Error('iteration failed');
    });
    expect(() => writeDismissed(keys)).not.toThrow();
  });
});

function renderStrip(items?: AttentionItem[]) {
  return render(<AttentionStrip items={items} />, { wrapper: MemoryRouter });
}

function withStorageFailure(failure: 'getter' | 'getItem' | 'setItem', run: () => void) {
  if (failure === 'getter') {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')!;
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() { throw new DOMException('denied', 'SecurityError'); },
    });
    try {
      run();
    } finally {
      Object.defineProperty(globalThis, 'localStorage', descriptor);
    }
    return;
  }

  const spy = vi.spyOn(localStorage, failure).mockImplementation(() => {
    throw new DOMException('denied', 'SecurityError');
  });
  try {
    run();
    expect(spy).toHaveBeenCalled();
  } finally {
    spy.mockRestore();
  }
}

describe('AttentionStrip', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedGet.mockResolvedValue([]);
  });

  test('渲染关注条目', () => {
    renderStrip(sample);
    expect(screen.getByText('svc-a 健康检查失败')).toBeTruthy();
    expect(screen.getByText('svc-b 有可更新版本')).toBeTruthy();
    expect(screen.getByText('2 项')).toBeTruthy();
  });

  test('空态显示今日无待关注', async () => {
    render(
      <MemoryRouter>
        <AttentionStrip refreshKey={1} />
      </MemoryRouter>
    );
    await waitFor(() => {
      expect(mockedGet).toHaveBeenCalled();
      expect(screen.getByText(/今日无待关注/)).toBeTruthy();
    });
  });

  test('今日忽略后隐藏条目', () => {
    renderStrip(sample);
    const key = attentionDismissKey(sample[0]);
    fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
    expect(screen.queryByText(sample[0].title)).toBeNull();
    expect(screen.getByText(sample[1].title)).toBeTruthy();

    const stored = JSON.parse(localStorage.getItem('console.attention.dismissed') || '{}');
    expect(stored.date).toBe(localDateKey());
    expect(stored.keys).toContain(key);
  });

  test('已忽略的条目启动时不显示', () => {
    writeDismissed(new Set([attentionDismissKey(sample[0])]));
    renderStrip(sample);
    expect(screen.queryByText(sample[0].title)).toBeNull();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });

  // A3：writeDismissed 内部裸调用 storage.setItem，隐私模式/配额异常会让它抛出。
  // dismiss()/dismissAll() 在 setDismissed 的函数式更新器里调用它，异常没被吞掉的话
  // 会让这次状态更新整体失败，用户点「今日忽略」看起来像没反应甚至让渲染报错。
  test('localStorage.setItem 抛异常（隐私模式）时点击「今日忽略」仍能在内存中生效', () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    try {
      renderStrip(sample);
      fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
      expect(screen.queryByText(sample[0].title)).toBeNull();
      expect(screen.getByText(sample[1].title)).toBeTruthy();
    } finally {
      vi.restoreAllMocks();
    }
  });
});

describe('AttentionStrip 一键已读', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedGet.mockResolvedValue([]);
  });

  test('点击全部已读后条目清空并写入今日忽略', () => {
    renderStrip(sample);
    expect(screen.getByText('2 项')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '全部已读' }));

    expect(screen.queryByText('svc-a 健康检查失败')).toBeNull();
    expect(screen.queryByText('svc-b 有可更新版本')).toBeNull();
    expect(screen.getByText('无待办')).toBeTruthy();
    // 持久化：两条 key 都进「今日忽略」，同日不再出现（次日自动回来）
    const dismissed = readDismissed();
    expect(dismissed.has(attentionDismissKey(sample[0]))).toBe(true);
    expect(dismissed.has(attentionDismissKey(sample[1]))).toBe(true);
  });

  test('无条目时不显示全部已读按钮', () => {
    renderStrip([]);
    expect(screen.queryByRole('button', { name: '全部已读' })).toBeNull();
  });
});

describe('续费已读与加载故障', () => {
  const expired: AttentionItem = {
    kind: 'renewal', severity: 'error', title: 'same 已过期',
    detail: '到期日 2026-09-24', link: '/renewals',
    dismiss_key: 'renewal|["one","2026-09-24","expired"]',
  };
  const upcoming: AttentionItem = {
    ...expired, severity: 'warn', title: 'same 即将到期',
    dismiss_key: 'renewal|["one","2026-09-25","upcoming"]',
  };

  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    vi.setSystemTime(new Date(2026, 8, 25, 12));
    mockedGet.mockResolvedValue([]);
  });
  afterEach(() => vi.useRealTimers());

  test('相同标题独立已读，改名保持已读，下一期重新出现', () => {
    const other = { ...expired, dismiss_key: 'renewal|["two","2026-09-24","expired"]' };
    const view = renderStrip([expired, other]);
    fireEvent.click(screen.getAllByLabelText(`忽略 ${expired.title}`)[0]);
    expect(screen.getAllByText(expired.title)).toHaveLength(1);
    view.rerender(<AttentionStrip items={[{ ...expired, title: 'renamed' }, other]} />);
    expect(screen.queryByText('renamed')).toBeNull();
    const next = { ...expired, dismiss_key: 'renewal|["one","2027-09-24","expired"]' };
    view.rerender(<AttentionStrip items={[next]} />);
    expect(screen.getByText(expired.title)).toBeTruthy();
  });

  test('仅过期提醒跨日已读，warn 与运维提醒次日回来', () => {
    const view = renderStrip([expired, upcoming, sample[0]]);
    expect(screen.getByRole('button', { name: `忽略 ${expired.title}` }).textContent).toContain('已读');
    fireEvent.click(screen.getByRole('button', { name: '全部已读' }));
    vi.setSystemTime(new Date(2026, 8, 26, 12));
    view.rerender(<AttentionStrip items={[expired, upcoming, sample[0]]} refreshKey={2} />);
    expect(screen.queryByText(expired.title)).toBeNull();
    expect(screen.getByText(upcoming.title)).toBeTruthy();
    expect(screen.getByText(sample[0].title)).toBeTruthy();
  });

  test('永久已读支持撤销与重挂载，旧标题键不会吞掉新身份', () => {
    localStorage.setItem('console.attention.acked', JSON.stringify(['renewal|same 已过期']));
    const view = renderStrip([expired]);
    fireEvent.click(screen.getByLabelText(`忽略 ${expired.title}`));
    fireEvent.click(screen.getByLabelText('撤销上次忽略或已读'));
    expect(screen.getByText(expired.title)).toBeTruthy();
    fireEvent.click(screen.getByLabelText(`忽略 ${expired.title}`));
    view.unmount();
    renderStrip([expired]);
    expect(screen.queryByText(expired.title)).toBeNull();
    expect(screen.queryByLabelText('撤销上次忽略或已读')).toBeNull();
  });

  test('存储被拒绝也保留本次永久已读与撤销', () => {
    withStorageFailure('getter', () => {
      renderStrip([expired]);
      fireEvent.click(screen.getByLabelText(`忽略 ${expired.title}`));
      expect(screen.queryByText(expired.title)).toBeNull();
      fireEvent.click(screen.getByLabelText('撤销上次忽略或已读'));
      expect(screen.getByText(expired.title)).toBeTruthy();
    });
  });

  test('初次失败显示错误，重试成功才显示空态', async () => {
    mockedGet.mockRejectedValueOnce(new Error('offline'));
    renderStrip();
    expect(screen.queryByText(/一切正常/)).toBeNull();
    await screen.findByRole('alert');
    expect(screen.queryByText(/一切正常/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await screen.findByText(/一切正常/);
  });

  test('刷新失败保留上次结果，较早请求失败不覆盖新响应', async () => {
    let rejectOld!: (reason: Error) => void;
    mockedGet.mockResolvedValueOnce(sample)
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectOld = reject; }))
      .mockResolvedValueOnce([upcoming])
      .mockRejectedValueOnce(new Error('offline'));
    const view = render(<AttentionStrip refreshKey={0} />, { wrapper: MemoryRouter });
    await screen.findByText(sample[0].title);
    view.rerender(<AttentionStrip refreshKey={1} />);
    view.rerender(<AttentionStrip refreshKey={2} />);
    await screen.findByText(upcoming.title);
    await act(async () => rejectOld(new Error('late error')));
    expect(screen.queryByRole('alert')).toBeNull();
    view.rerender(<AttentionStrip refreshKey={3} />);
    await screen.findByRole('alert');
    expect(screen.getByText(upcoming.title)).toBeTruthy();
    expect(screen.queryByText(/一切正常/)).toBeNull();
  });
});

describe('F14 本地日期与内存忽略', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedGet.mockResolvedValue([]);
    vi.stubEnv('TZ', 'Asia/Shanghai');
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-09-08T15:59:00Z')); // 本地 23:59
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  test('存储日期采用本地日历，UTC 仍是昨日时也写入新的一天', () => {
    vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
    expect(localDateKey()).toBe('2026-09-09');
    expect(new Date().toISOString().slice(0, 10)).toBe('2026-09-08');
    renderStrip(sample);
    fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
    expect(JSON.parse(localStorage.getItem('console.attention.dismissed')!).date).toBe('2026-09-09');
  });

  test('同日多次刷新保留忽略，次日刷新后昨日条目重新显示', () => {
    const { rerender } = renderStrip(sample);
    fireEvent.click(screen.getByRole('button', { name: '全部已读' }));
    for (const refreshKey of [1, 2]) {
      rerender(<AttentionStrip items={sample} refreshKey={refreshKey} />);
      expect(screen.getByText('无待办')).toBeTruthy();
      expect(screen.queryByText(sample[0].title)).toBeNull();
    }

    vi.setSystemTime(new Date('2026-09-08T16:01:00Z')); // 本地次日 00:01
    rerender(<AttentionStrip items={sample} refreshKey={3} />);
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
    expect(screen.queryByText(/一切正常/)).toBeNull();

    fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
    rerender(<AttentionStrip items={sample} refreshKey={4} />);
    expect(screen.queryByText(sample[0].title)).toBeNull();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });

  test('跨日刷新在 API 尚未返回时已恢复昨日忽略项', () => {
    const { rerender } = renderStrip(sample);
    fireEvent.click(screen.getByRole('button', { name: '全部已读' }));
    mockedGet.mockReturnValueOnce(new Promise(() => {}));
    vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
    rerender(<AttentionStrip refreshKey={1} />);
    expect(mockedGet).toHaveBeenCalledTimes(1);
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });

  test('刷新请求跨午夜返回时也校准忽略日期', async () => {
    let resolveAttention!: (items: AttentionItem[]) => void;
    const { rerender } = renderStrip(sample);
    fireEvent.click(screen.getByRole('button', { name: '全部已读' }));
    mockedGet.mockReturnValueOnce(new Promise((resolve) => { resolveAttention = resolve; }));
    rerender(<AttentionStrip refreshKey={1} />);
    vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
    await act(async () => { resolveAttention(sample); });
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });

  test.each(['单项忽略', '全部已读'] as const)('跨日后直接%s，不把昨日 keys 重新标成今天', (action) => {
    renderStrip(sample);
    fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
    vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
    fireEvent.click(action === '单项忽略'
      ? screen.getByLabelText(`忽略 ${sample[1].title}`)
      : screen.getByRole('button', { name: '全部已读' }));

    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.queryByText(sample[1].title)).toBeNull();
    expect(JSON.parse(localStorage.getItem('console.attention.dismissed')!)).toEqual({
      date: '2026-09-09',
      keys: [attentionDismissKey(sample[1])],
    });
  });

  test.each(['getter', 'getItem', 'setItem'] as const)('%s 持续拒绝时同日多次刷新保留内存，跨日仍重置且可继续忽略', (failure) => {
    withStorageFailure(failure, () => {
      const { rerender } = renderStrip(sample);
      fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
      for (const refreshKey of [1, 2]) {
        rerender(<AttentionStrip items={sample} refreshKey={refreshKey} />);
        expect(screen.queryByText(sample[0].title)).toBeNull();
        expect(screen.getByText(sample[1].title)).toBeTruthy();
      }
      vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
      rerender(<AttentionStrip items={sample} refreshKey={3} />);
      expect(screen.getByText(sample[0].title)).toBeTruthy();
      expect(screen.getByText(sample[1].title)).toBeTruthy();
      fireEvent.click(screen.getByLabelText(`忽略 ${sample[1].title}`));
      rerender(<AttentionStrip items={sample} refreshKey={4} />);
      expect(screen.getByText(sample[0].title)).toBeTruthy();
      expect(screen.queryByText(sample[1].title)).toBeNull();
    });
  });

  test('同日重挂载沿用存储，跨日重挂载不读取昨日忽略', () => {
    const first = renderStrip(sample);
    fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
    first.unmount();
    const second = renderStrip(sample);
    expect(screen.queryByText(sample[0].title)).toBeNull();
    second.unmount();
    vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
    renderStrip(sample);
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });
});

describe('F19 本页最近一次忽略撤销', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedGet.mockResolvedValue([]);
    vi.stubEnv('TZ', 'Asia/Shanghai');
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-09-08T15:59:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  const undoButton = () => screen.getByRole('button', { name: /撤销/ });
  const dismissFirst = () => fireEvent.click(screen.getByLabelText(`忽略 ${sample[0].title}`));
  const dismissSecond = () => fireEvent.click(screen.getByLabelText(`忽略 ${sample[1].title}`));

  test('单项忽略可撤销一次，刷新后也保留操作前集合', () => {
    const { rerender } = renderStrip(sample);
    expect(screen.queryByRole('button', { name: /撤销/ })).toBeNull();
    dismissFirst();
    rerender(<AttentionStrip items={sample} refreshKey={1} />);
    fireEvent.click(undoButton());
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /撤销/ })).toBeNull();
    expect(readDismissed()).toEqual(new Set());
    rerender(<AttentionStrip items={sample} refreshKey={2} />);
    expect(screen.getByText(sample[0].title)).toBeTruthy();
  });

  test.each(['单项忽略', '全部已读'] as const)('下一次%s 替换快照，撤销不会继续撤回更早的忽略', (action) => {
    renderStrip(sample);
    dismissFirst();
    fireEvent.click(action === '单项忽略'
      ? screen.getByLabelText(`忽略 ${sample[1].title}`)
      : screen.getByRole('button', { name: '全部已读' }));
    expect(screen.getByText('无待办')).toBeTruthy();
    fireEvent.click(undoButton());
    expect(screen.queryByText(sample[0].title)).toBeNull();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
    expect(readDismissed()).toEqual(new Set([attentionDismissKey(sample[0])]));
    expect(screen.queryByRole('button', { name: /撤销/ })).toBeNull();
  });

  test.each([false, true])('全部已读后入口仍可达，折叠=%s 时撤销也保留折叠状态', (collapsed) => {
    renderStrip(sample);
    const heading = screen.getByRole('button', { name: /今日关注/ });
    if (collapsed) fireEvent.click(heading);
    fireEvent.click(screen.getByRole('button', { name: '全部已读' }));
    expect(screen.getByText('无待办')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '全部已读' })).toBeNull();
    fireEvent.click(undoButton());
    expect(screen.getByText('2 项')).toBeTruthy();
    expect(heading.getAttribute('aria-expanded')).toBe(String(!collapsed));
    if (collapsed) {
      expect(screen.queryByText(sample[0].title)).toBeNull();
      fireEvent.click(heading);
    }
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });

  test('撤销恢复完整的操作前 Set，保留先前存储的非当前条目 key', () => {
    const before = new Set(['probe|已离开列表的服务', attentionDismissKey(sample[0])]);
    writeDismissed(before);
    renderStrip(sample);
    dismissSecond();
    fireEvent.click(undoButton());
    expect(readDismissed()).toEqual(before);
    expect(screen.queryByText(sample[0].title)).toBeNull();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });

  test('重挂载不保留撤销历史，存储中只保留当前忽略集合', () => {
    const first = renderStrip(sample);
    dismissFirst();
    expect(undoButton()).toBeTruthy();
    expect(JSON.parse(localStorage.getItem('console.attention.dismissed')!)).toEqual({
      date: '2026-09-08', keys: [attentionDismissKey(sample[0])],
    });
    first.unmount();
    renderStrip(sample);
    expect(screen.queryByRole('button', { name: /撤销/ })).toBeNull();
    expect(screen.queryByText(sample[0].title)).toBeNull();
    dismissSecond();
    fireEvent.click(undoButton());
    expect(screen.queryByText(sample[0].title)).toBeNull();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });

  test('跨日刷新同时清除忽略和撤销历史', () => {
    const { rerender } = renderStrip(sample);
    dismissFirst();
    dismissSecond();
    expect(undoButton()).toBeTruthy();
    vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
    rerender(<AttentionStrip items={sample} refreshKey={1} />);
    expect(screen.queryByRole('button', { name: /撤销/ })).toBeNull();
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
  });

  test('没有刷新时点击跨日残留的撤销按钮，也不能恢复昨日集合', () => {
    renderStrip(sample);
    dismissFirst();
    dismissSecond(); // 最新快照包含昨日已忽略的第一项，不能恢复它。
    const staleButton = undoButton();
    vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
    fireEvent.click(staleButton);
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /撤销/ })).toBeNull();
    expect(readDismissed()).toEqual(new Set());
  });

  test.each(['单项忽略', '全部已读'] as const)('跨日后%s 再撤销，快照不包含昨日忽略', (action) => {
    renderStrip(sample);
    dismissFirst();
    vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
    fireEvent.click(action === '单项忽略'
      ? screen.getByLabelText(`忽略 ${sample[1].title}`)
      : screen.getByRole('button', { name: '全部已读' }));
    fireEvent.click(undoButton());
    expect(screen.getByText(sample[0].title)).toBeTruthy();
    expect(screen.getByText(sample[1].title)).toBeTruthy();
    expect(JSON.parse(localStorage.getItem('console.attention.dismissed')!)).toEqual({
      date: '2026-09-09', keys: [],
    });
  });

  test.each(['getter', 'getItem', 'setItem'] as const)('%s 持续拒绝时能内存撤销，跨日旧快照作废后仍可操作', (failure) => {
    withStorageFailure(failure, () => {
      const { rerender } = renderStrip(sample);
      dismissFirst();
      rerender(<AttentionStrip items={sample} refreshKey={1} />);
      fireEvent.click(undoButton());
      expect(screen.getByText(sample[0].title)).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: '全部已读' }));
      fireEvent.click(undoButton());
      expect(screen.getByText('2 项')).toBeTruthy();

      dismissFirst();
      dismissSecond();
      vi.setSystemTime(new Date('2026-09-08T16:01:00Z'));
      fireEvent.click(undoButton());
      expect(screen.getByText('2 项')).toBeTruthy();
      expect(screen.queryByRole('button', { name: /撤销/ })).toBeNull();
      dismissFirst();
      fireEvent.click(undoButton());
      expect(screen.getByText('2 项')).toBeTruthy();
    });
  });
});
