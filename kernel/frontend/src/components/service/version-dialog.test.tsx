import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { VersionDialog } from './version-dialog';
import { getVersionDetail, saveUpgradeReview } from '@/lib/api';
import type { VersionDetail } from '@/lib/types';

vi.mock('@/lib/api', () => ({
  getVersionDetail: vi.fn(),
  saveUpgradeReview: vi.fn(),
  apiErrorMessage: (error: Error) => error.message,
}));
const getDetail = vi.mocked(getVersionDetail);
const saveReview = vi.mocked(saveUpgradeReview);
const onOpenChange = vi.fn();

function deferred() {
  let resolve!: (value: VersionDetail) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<VersionDetail>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetAllMocks();
  getDetail.mockResolvedValue({ local: 'v2', upstream: 'v3' });
});

afterEach(async () => {
  cleanup();
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
});

test('概览和版本区间统一省略 v，Release 原文和比较链接保留', async () => {
  const compareUrl = 'https://github.com/example/service/compare/v0.1.1867...v0.1.1868';
  getDetail.mockResolvedValue({
    local: 'v0.1.1867',
    upstream: 'V0.1.1868',
    compare_url: compareUrl,
    changes: [{ sha: 'abcdef0', msg: '更新服务' }],
    release_notes: 'v0.1.1868 发布说明',
  });
  render(<VersionDialog open name="svc" onOpenChange={onOpenChange} />);
  await screen.findByText('0.1.1867');
  expect(screen.getByText('0.1.1868')).toBeTruthy();
  expect(screen.getByRole('link', { name: '在 GitHub 查看 →' }).getAttribute('href')).toBe(compareUrl);
  fireEvent.click(screen.getByRole('button', { name: /版本区间更新/ }));
  expect(screen.getByText('0.1.1867 → 0.1.1868')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '最新 Release 说明' }));
  expect(screen.getByText('v0.1.1868 发布说明')).toBeTruthy();
});

test('切服务后旧响应不覆盖当前服务详情', async () => {
  const old = deferred();
  getDetail.mockReturnValueOnce(old.promise);
  const { rerender } = render(<VersionDialog open name="first" onOpenChange={onOpenChange} />);
  rerender(<VersionDialog open name="second" onOpenChange={onOpenChange} />);
  await screen.findByText('2');
  await act(async () => old.resolve({ local: 'old-version' }));
  expect(screen.queryByText('old-version')).toBeNull();
  expect(screen.getByText('2')).toBeTruthy();
});

test('关闭再打开后旧失败不能污染新详情', async () => {
  const old = deferred();
  getDetail.mockReturnValueOnce(old.promise);
  const { rerender } = render(<VersionDialog open name="first" onOpenChange={onOpenChange} />);
  rerender(<VersionDialog open={false} name="first" onOpenChange={onOpenChange} />);
  rerender(<VersionDialog open name="first" onOpenChange={onOpenChange} />);
  await screen.findByText('2');
  await act(async () => old.reject(new Error('old failure')));
  expect(screen.queryByText('old failure')).toBeNull();
  expect(screen.getByText('2')).toBeTruthy();
});

test('同步网络失败保留版本详情，并可再次同步', async () => {
  render(<VersionDialog open name="first" onOpenChange={onOpenChange} />);
  await screen.findByText('2');
  getDetail.mockRejectedValueOnce(new Error('offline'));
  fireEvent.click(screen.getByRole('button', { name: '刷新同步' }));
  await screen.findByRole('alert');
  expect(screen.getByText('2')).toBeTruthy();
  getDetail.mockResolvedValueOnce({ local: 'v4' });
  fireEvent.click(screen.getByRole('button', { name: '刷新同步' }));
  await screen.findByText('4');
  expect(screen.queryByRole('alert')).toBeNull();
  expect(getDetail).toHaveBeenLastCalledWith('first', true);
});

test('API 返回错误也保留已有详情，切服务则清旧详情', async () => {
  const { rerender } = render(<VersionDialog open name="first" onOpenChange={onOpenChange} />);
  await screen.findByText('2');
  getDetail.mockResolvedValueOnce({ error: 'rate limit' });
  fireEvent.click(screen.getByRole('button', { name: '刷新同步' }));
  await screen.findByRole('alert');
  expect(screen.getByText('2')).toBeTruthy();
  getDetail.mockRejectedValueOnce(new Error('second failed'));
  rerender(<VersionDialog open name="second" onOpenChange={onOpenChange} />);
  await screen.findByText('second failed');
  expect(screen.queryByText('2')).toBeNull();
});

const PREP: VersionDetail['upgrade_prep'] = {
  review: { status: 'pending', reason: '', checks: {}, decided_at: null, updated_at: null },
  checks: [
    { key: 'config', label: '配置副本与构建上下文已保留', done: false },
    { key: 'data', label: '持久数据一致性备份已审阅', done: false },
  ],
  changes: [
    { category: '配置', severity: 'breaking', excerpt: 'Removed deprecated config option', line: 2 },
  ],
  category: 'breaking',
  comparison: 'snapshot_differs',
  blockers: ['配置副本与构建上下文已保留'],
  readiness: 'needs_review',
};

test('升级准备区块：展示影响分析与清单，勾选后保存并回写', async () => {
  getDetail.mockResolvedValue({ local: 'v2', upstream: 'v3', upgrade_prep: PREP });
  saveReview.mockResolvedValue({
    status: 'pending', reason: '', checks: { config: true }, decided_at: '2026-10-17 12:00:00', updated_at: '2026-10-17 12:00:00',
  });
  render(<VersionDialog open name="svc" onOpenChange={onOpenChange} />);
  fireEvent.click(await screen.findByRole('button', { name: /升级影响与准备/ }));
  expect(screen.getByText('含破坏性变更候选')).toBeTruthy();
  expect(screen.getByText('Removed deprecated config option')).toBeTruthy();
  // readiness 徽标与状态下拉同文案，取徽标那一处
  expect(screen.getAllByText('待审阅').length).toBeGreaterThan(0);

  const saveButton = screen.getByRole('button', { name: '保存审阅' });
  expect((saveButton as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('checkbox', { name: '配置副本与构建上下文已保留' }));
  expect((saveButton as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(saveButton);
  await screen.findByText('上次保存：', { exact: false });
  expect(saveReview).toHaveBeenCalledWith('svc', {
    status: 'pending', reason: '', checks: { config: true },
  });
});

test('选择忽略必须填理由；保存失败就地提示且表单不回退', async () => {
  getDetail.mockResolvedValue({ local: 'v2', upstream: 'v3', upgrade_prep: PREP });
  saveReview.mockRejectedValue(new Error('忽略或延后必须填写理由'));
  render(<VersionDialog open name="svc" onOpenChange={onOpenChange} />);
  fireEvent.click(await screen.findByRole('button', { name: /升级影响与准备/ }));

  fireEvent.change(screen.getByRole('combobox', { name: '审阅状态' }), { target: { value: 'ignored' } });
  fireEvent.change(screen.getByRole('textbox', { name: '理由' }), { target: { value: '等下个大版本' } });
  fireEvent.click(screen.getByRole('button', { name: '保存审阅' }));
  await screen.findByRole('alert');
  expect(screen.getByText('忽略或延后必须填写理由')).toBeTruthy();
  // 表单不回退到已持久化状态
  expect((screen.getByRole('combobox', { name: '审阅状态' }) as HTMLSelectElement).value).toBe('ignored');
});

test('长内容按项独立折叠，折叠升级准备保留未保存的审阅草稿', async () => {
  getDetail.mockResolvedValue({
    local: 'v2', upstream: 'v3', upgrade_prep: PREP,
    changes: [{ sha: 'abcdef0', msg: '调整服务配置' }],
    release_notes: '完整 Release 说明',
  });
  render(<VersionDialog open name="svc" onOpenChange={onOpenChange} />);
  const overview = await screen.findByRole('button', { name: '版本概览' });
  const changes = screen.getByRole('button', { name: /版本区间更新/ });
  const release = screen.getByRole('button', { name: '最新 Release 说明' });
  const preparation = screen.getByRole('button', { name: /升级影响与准备/ });
  const content = (button: HTMLElement) => document.getElementById(button.getAttribute('aria-controls')!)!;

  expect(overview.getAttribute('aria-expanded')).toBe('true');
  for (const button of [changes, release, preparation]) {
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(content(button).hidden).toBe(true);
  }
  fireEvent.click(changes);
  fireEvent.click(release);
  expect(content(changes).hidden).toBe(false);
  expect(content(release).hidden).toBe(false);

  fireEvent.click(preparation);
  const checkbox = screen.getByRole('checkbox', { name: '配置副本与构建上下文已保留' });
  fireEvent.click(checkbox);
  fireEvent.change(screen.getByRole('combobox', { name: '审阅状态' }), { target: { value: 'deferred' } });
  fireEvent.change(screen.getByRole('textbox', { name: '理由' }), { target: { value: '下周维护窗口处理' } });
  fireEvent.click(preparation);
  expect(screen.queryByRole('checkbox')).toBeNull();
  fireEvent.click(preparation);

  expect(screen.getByRole('checkbox', { name: '配置副本与构建上下文已保留' })).toBe(checkbox);
  expect((checkbox as HTMLInputElement).checked).toBe(true);
  expect((screen.getByRole('combobox', { name: '审阅状态' }) as HTMLSelectElement).value).toBe('deferred');
  expect((screen.getByRole('textbox', { name: '理由' }) as HTMLInputElement).value).toBe('下周维护窗口处理');
  expect((screen.getByRole('button', { name: '保存审阅' }) as HTMLButtonElement).disabled).toBe(false);
  expect(saveReview).not.toHaveBeenCalled();
  expect(content(changes).hidden).toBe(false);
  expect(content(release).hidden).toBe(false);
});
