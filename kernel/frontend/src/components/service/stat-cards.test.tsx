import { describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { StatCards } from './stat-cards';

describe('StatCards chip 化（D5）', () => {
  test('渲染 4 个统计 chip 与计数', () => {
    render(
      <StatCards total={10} running={8} healthy={7} outdated={2} active={null} onSelect={() => {}} />
    );
    expect(screen.getByText('服务总数')).toBeTruthy();
    expect(screen.getByText('运行中')).toBeTruthy();
    expect(screen.getByText('健康')).toBeTruthy();
    expect(screen.getByText('待更新')).toBeTruthy();
    expect(screen.getByText('10')).toBeTruthy();
    expect(screen.getByText('8')).toBeTruthy();
    expect(screen.getByText('7')).toBeTruthy();
    expect(screen.getByText('2')).toBeTruthy();
  });

  test('点击 chip 筛选，再次点击取消（受控：父组件更新 active 后重渲染）', () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <StatCards total={1} running={1} healthy={1} outdated={1} active={null} onSelect={onSelect} />
    );
    const runningChip = screen.getByText('运行中').closest('button')!;
    fireEvent.click(runningChip);
    expect(onSelect).toHaveBeenNthCalledWith(1, 'running');
    // 父组件收到后置 active='running'，rerender 反映激活态
    rerender(
      <StatCards total={1} running={1} healthy={1} outdated={1} active="running" onSelect={onSelect} />
    );
    fireEvent.click(screen.getByText('运行中').closest('button')!);
    expect(onSelect).toHaveBeenNthCalledWith(2, null);
  });

  test('active 时 aria-pressed 置位', () => {
    render(
      <StatCards total={1} running={1} healthy={1} outdated={1} active="running" onSelect={() => {}} />
    );
    const runningChip = screen.getByText('运行中').closest('button')!;
    expect(runningChip.getAttribute('aria-pressed')).toBe('true');
  });

  test('loading 时骨架与徽章底色不同，否则 loading 态不可见', () => {
    const { container } = render(
      <StatCards total={0} running={0} healthy={0} outdated={0} active={null} onSelect={() => {}} loading />
    );
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBe(4);
    skeletons.forEach((s) => {
      // 徽章是 bg-muted，骨架必须区别于它
      expect(s.className).toContain('bg-muted-foreground/20');
    });
  });

  test('offlineServers undefined 时不渲染离线 chip（向后兼容）', () => {
    render(
      <StatCards total={1} running={1} healthy={1} outdated={1} active={null} onSelect={() => {}} />
    );
    expect(screen.queryByText('服务器离线')).toBeNull();
  });

  test('offlineServers=0 时不渲染离线 chip（避免「离线 0」噪声）', () => {
    render(
      <StatCards
        total={1}
        running={1}
        healthy={1}
        outdated={1}
        active={null}
        onSelect={() => {}}
        offlineServers={0}
      />
    );
    expect(screen.queryByText('服务器离线')).toBeNull();
  });

  test('offlineServers>0 渲染离线 chip 并触发 onOfflineClick', () => {
    const onOfflineClick = vi.fn();
    render(
      <StatCards
        total={1}
        running={1}
        healthy={1}
        outdated={1}
        active={null}
        onSelect={() => {}}
        offlineServers={2}
        onOfflineClick={onOfflineClick}
      />
    );
    expect(screen.getByText('服务器离线')).toBeTruthy();
    expect(screen.getByText('2')).toBeTruthy();
    fireEvent.click(screen.getByText('服务器离线'));
    expect(onOfflineClick).toHaveBeenCalledOnce();
    expect(screen.getByText('服务器离线').closest('button')).toBeTruthy();
  });

  test('无 onOfflineClick 时离线 chip 退化为非交互 span（不做点了没反应的假按钮）', () => {
    render(
      <StatCards
        total={1}
        running={1}
        healthy={1}
        outdated={1}
        active={null}
        onSelect={() => {}}
        offlineServers={2}
      />
    );
    const chip = screen.getByText('服务器离线');
    expect(chip).toBeTruthy();
    expect(chip.closest('button')).toBeNull();
  });
});
