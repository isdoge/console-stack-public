import { fireEvent, render, screen } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import { allowedActions, ServiceActions } from './service-actions';

test('已停止服务显示启动，运行中服务显示停止，显式限制保留', () => {
  const onAction = vi.fn();
  const { unmount } = render(<ServiceActions svc={{name:'web',machine:'nas',running:'stopped'}} onSettings={vi.fn()} onAction={onAction} />);
  fireEvent.click(screen.getByRole('button', {name:'操作'}));
  fireEvent.keyDown(screen.getByRole('button', {name:'操作'}), {key:'ArrowDown'});
  fireEvent.click(screen.getByRole('menuitem', {name:'启动'}));
  expect(onAction).toHaveBeenCalledWith(expect.objectContaining({name:'web'}), 'start');
  expect(screen.queryByRole('menuitem', {name:'停止'})).toBeNull();
  unmount();
  render(<ServiceActions svc={{name:'web',machine:'nas',running:'running',actions:['stop','logs']}} onSettings={vi.fn()} onAction={onAction} />);
  fireEvent.keyDown(screen.getByRole('button', {name:'操作'}), {key:'ArrowDown'});
  expect(screen.getByRole('menuitem', {name:'停止'})).toBeTruthy();
  expect(screen.queryByRole('menuitem', {name:'启动'})).toBeNull();
  expect(screen.queryByRole('menuitem', {name:'重启'})).toBeNull();
});

test('无管理单元的进程无虚假 Docker 动作，空白名单仍然关闭', () => {
  expect([...allowedActions({name:'web',machine:'nas',type:'process',actions:['restart','logs']})]).toEqual([]);
  expect([...allowedActions({name:'db',machine:'nas',actions:[]})]).toEqual([]);
});
