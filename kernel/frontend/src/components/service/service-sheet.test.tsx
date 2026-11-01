import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import * as api from '@/lib/api';
import { formFromHealthProbe, ServiceSheet } from './service-sheet';

vi.mock('@/lib/api', () => ({
  deleteService: vi.fn(),
  extractPathSuffix: vi.fn(() => ''),
  saveService: vi.fn(),
  suggestAccess: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const mockedSave = vi.mocked(api.saveService);

beforeEach(() => {
  vi.clearAllMocks();
  mockedSave.mockResolvedValue({ ok: true });
});

afterEach(async () => {
  cleanup();
  // Radix 在卸载后异步恢复焦点，须在 jsdom 环境拆除前等这一轮回调完成。
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
});

describe('ServiceSheet machine selection', () => {
  test('displays Oracle but submits the stable us machine id', async () => {
    render(
      <ServiceSheet
        open
        mode="add"
        defaultMachineId="us"
        machines={[
          { id: 'nas', label: 'NAS', role: 'server' },
          { id: 'us', label: 'Oracle', role: 'server' },
        ]}
        onOpenChange={vi.fn()}
        onSaved={vi.fn()}
      />
    );

    const machineSelect = screen.getByRole('combobox', { name: '所属主机' });
    const oracleOption = screen.getByRole('option', { name: 'Oracle' }) as HTMLOptionElement;
    expect(oracleOption.value).toBe('us');
    expect((machineSelect as HTMLSelectElement).value).toBe('us');

    const nameInput = screen.getByText('服务名称').parentElement?.querySelector('input');
    if (!nameInput) throw new Error('找不到服务名称输入框');
    fireEvent.change(nameInput, { target: { value: 'axonhub-oracle' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并探测' }));

    await waitFor(() => expect(mockedSave).toHaveBeenCalledOnce());
    expect(mockedSave.mock.calls[0][0].machine).toBe('us');
  });
});

describe('L1 健康检查三态', () => {
  const renderSheet = (service?: Parameters<typeof ServiceSheet>[0]['service']) =>
    render(
      <ServiceSheet
        open
        mode={service ? 'edit' : 'add'}
        service={service}
        defaultMachineId="nas"
        machines={[{ id: 'nas', label: 'NAS', role: 'server' }]}
        onOpenChange={vi.fn()}
        onSaved={vi.fn()}
      />
    );

  test('默认模式不显示语义字段', () => {
    renderSheet();
    expect(screen.queryByLabelText('期望状态码')).toBeNull();
  });

  test('切到 HTTP 语义后展开四个字段并提交成对象', async () => {
    renderSheet();
    const nameInput = screen.getByText('服务名称').parentElement?.querySelector('input');
    if (!nameInput) throw new Error('找不到服务名称输入框');
    fireEvent.change(nameInput, { target: { value: 'api' } });

    fireEvent.click(screen.getByRole('button', { name: 'HTTP 语义' }));
    fireEvent.change(screen.getByLabelText('最大耗时毫秒'), { target: { value: '1500' } });
    fireEvent.change(screen.getByLabelText('响应体须包含'), { target: { value: '"ok"' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并探测' }));

    await waitFor(() => expect(mockedSave).toHaveBeenCalledOnce());
    const form = mockedSave.mock.calls[0][0];
    expect(form.health_probe_mode).toBe('http');
    expect(form.health_probe_status).toBe('200');
    expect(form.health_probe_latency).toBe('1500');
    expect(form.health_probe_body).toBe('"ok"');
  });

  test('编辑带语义配置的服务时把它回填出来（不回填就会被保存抹掉）', () => {
    renderSheet({
      name: 'api',
      machine: 'nas',
      health_probe: {
        type: 'http',
        target: 'tailscale',
        expected_status: 204,
        max_latency_ms: 800,
        body_contains: 'pong',
      },
    });
    expect((screen.getByLabelText('期望状态码') as HTMLInputElement).value).toBe('204');
    expect((screen.getByLabelText('最大耗时毫秒') as HTMLInputElement).value).toBe('800');
    expect((screen.getByLabelText('响应体须包含') as HTMLInputElement).value).toBe('pong');
    expect(screen.getByRole('button', { name: 'Tailscale' }).getAttribute('aria-pressed')).toBe(
      'true'
    );
  });

  test('health_probe: false 回填成「关闭」', () => {
    renderSheet({ name: 'db', machine: 'nas', health_probe: false });
    expect(screen.getByRole('button', { name: '关闭' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.queryByLabelText('期望状态码')).toBeNull();
  });
});

describe('formFromHealthProbe（台账三态 → 表单）', () => {
  test('false → 关闭', () => {
    expect(formFromHealthProbe(false).health_probe_mode).toBe('off');
  });

  test('缺省 / true → 默认推断', () => {
    expect(formFromHealthProbe(undefined).health_probe_mode).toBe('default');
    expect(formFromHealthProbe(true).health_probe_mode).toBe('default');
  });

  test('对象 → HTTP 语义，null 字段回填成空串（对应「不校验」）', () => {
    expect(
      formFromHealthProbe({
        type: 'http',
        target: 'home',
        expected_status: null,
        max_latency_ms: 900,
      })
    ).toEqual({
      health_probe_mode: 'http',
      health_probe_target: 'home',
      health_probe_status: '',
      health_probe_latency: '900',
      health_probe_body: '',
    });
  });

  test('写坏的对象按「默认推断」回填，不是「关闭」（配错 ≠ 关掉）', () => {
    expect(
      formFromHealthProbe({ type: 'tcp' } as unknown as Parameters<
        typeof formFromHealthProbe
      >[0]).health_probe_mode
    ).toBe('default');
  });

  test('非法 target 退回 domain，否则三个按钮一个都不高亮', () => {
    const fields = formFromHealthProbe({
      type: 'http',
      target: 'somewhere',
    } as unknown as Parameters<typeof formFromHealthProbe>[0]);
    expect(fields.health_probe_target).toBe('domain');
  });
});

test('主机进程管理字段和启停白名单随设置保存', async () => {
  render(<ServiceSheet open mode="edit" service={{name:'token-scope',machine:'nas',type:'process',systemd_unit:'token-scope.service',systemd_scope:'system',actions:['restart','logs']}}
    machines={[{id:'nas',label:'NAS',role:'server'}]} onOpenChange={vi.fn()} onSaved={vi.fn()} />);
  expect((screen.getByLabelText('systemd 服务单元') as HTMLInputElement).value).toBe('token-scope.service');
  fireEvent.click(screen.getByLabelText('允许启动'));
  fireEvent.click(screen.getByLabelText('允许停止'));
  fireEvent.click(screen.getByRole('button', {name:'保存并探测'}));
  await waitFor(() => expect(mockedSave).toHaveBeenCalledOnce());
  expect(mockedSave.mock.calls[0][0]).toMatchObject({type:'process',systemd_unit:'token-scope.service',actions:['restart','logs','start','stop']});
});
