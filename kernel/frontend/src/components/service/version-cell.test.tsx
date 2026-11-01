import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { VersionCell } from './version-cell';
import type { Service } from '@/lib/types';

afterEach(cleanup);

test.each(['v0.1.1867', '0.1.1867', 'V0.1.1867'])(
  '列表和更新提示统一省略版本前缀：%s',
  (local) => {
    const svc: Service = {
      name: 'magpie', machine: 'gz',
      version_info: { local, upstream: 'v0.1.1868' },
    };
    const onOpen = vi.fn();
    render(<VersionCell svc={svc} onOpen={onOpen} />);
    const button = screen.getByRole('button', { name: '0.1.1867' });
    expect(button.title).toBe('有新版本 0.1.1868，点击查看详情');
    fireEvent.click(button);
    expect(onOpen).toHaveBeenCalledWith(svc);
    expect(svc.version_info?.local).toBe(local);
    expect(svc.version_info?.upstream).toBe('v0.1.1868');
  },
);

test.each(['latest', 'N/A', 'vNext', 'dev-abcdef0'])(
  '非数字版本标识保持原样：%s',
  (local) => {
    render(<VersionCell svc={{ name: 'custom', machine: 'nas', version_info: { local } }} />);
    expect(screen.getByRole('button', { name: local })).toBeTruthy();
  },
);
