import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { toast } from 'sonner';
import * as api from '@/lib/api';
import type { Machine } from '@/lib/types';
import { MachineSheet } from './machine-sheet';

vi.mock('@/lib/api', () => ({
  getMachineForEdit: vi.fn(),
  saveMachine: vi.fn(),
  deleteMachine: vi.fn(),
  // catch 里的错误提取 helper 随模块一起被 mock，行为对齐真实实现的鸭子类型路径
  apiErrorMessage: (err: { response?: { data?: { error?: string } }; message?: string }) =>
    err?.response?.data?.error ?? err?.message ?? '',
  // upgrade.proxy_env 回填：与真实实现等价的简易版（编辑表单只读不写）
  formatProxyEnvText: (env?: Record<string, string> | null) =>
    !env ? '' : Object.keys(env).sort().map((k) => `${k}=${env[k]}`).join('\n'),
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const mockedGet = vi.mocked(api.getMachineForEdit);
const mockedSave = vi.mocked(api.saveMachine);
const mockedDelete = vi.mocked(api.deleteMachine);
const mockedError = vi.mocked(toast.error);
const mockedSuccess = vi.mocked(toast.success);

const KR_FULL: Machine = {
  id: 'kr',
  label: '光帆 KR',
  role: 'node',
  provider: 'AWS Lightsail 韩国',
  os: 'Ubuntu 24.04',
  region: '🇰🇷',
  public_host: 'kr.sunxc.eu.cc',
  tailscale: '100.64.0.50',
  ssh: 'admin@100.64.0.50',
  ssh_key: 'aws-kr.pem',
  beszel_name: 'KR',
  capabilities: ['beszel', 'ssh'],
  probe: { type: 'tcp', port: 62037 },
  traffic_quota_gb: 1000,
  traffic_nic: 'ens5',
  renewal_ref: 'node-kr',
  systemd_units: ['sing-box'],
};

/** 表单控件按 label 文案取，避免依赖 DOM 结构（本仓库没装 jest-dom，断言走原生属性）。 */
function fieldInput(label: string): HTMLInputElement {
  const input = screen.getByText(label).parentElement?.querySelector('input');
  if (!input) throw new Error(`找不到字段: ${label}`);
  return input as HTMLInputElement;
}

function fieldValue(label: string): string {
  return fieldInput(label).value;
}

function setField(label: string, value: string) {
  fireEvent.change(fieldInput(label), { target: { value } });
}

/** 多行文本框（systemd 白名单）：Field 的 Label 下挂的是 textarea。 */
function fieldArea(label: string): HTMLTextAreaElement {
  const area = screen.getByText(label).parentElement?.querySelector('textarea');
  if (!area) throw new Error(`找不到多行字段: ${label}`);
  return area as HTMLTextAreaElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedGet.mockResolvedValue(KR_FULL);
  mockedSave.mockResolvedValue({ ok: true });
  mockedDelete.mockResolvedValue(undefined);
});

describe('MachineSheet 编辑模式（B3 §5.3）', () => {
  test('打开时按 ?full=1 拉全量并填满表单', async () => {
    render(
      <MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />
    );
    await waitFor(() => expect(mockedGet).toHaveBeenCalledWith('kr'));
    await waitFor(() => expect(fieldValue('显示名')).toBe('光帆 KR'));
    // 列表接口不带的三项必须出现在表单里，否则保存会清空
    expect(fieldValue('Tailscale IP')).toBe('100.64.0.50');
    expect(fieldValue('SSH 目标')).toBe('admin@100.64.0.50');
    expect(fieldValue('SSH 密钥文件名')).toBe('aws-kr.pem');
    // 嵌套/数值字段展平成字符串
    expect(fieldValue('探活端口（tcp）')).toBe('62037');
    expect(fieldValue('月流量配额 (GB)')).toBe('1000');
    expect(fieldValue('计费网卡')).toBe('ens5');
    expect(fieldValue('续费条目 id')).toBe('node-kr');
    // §4.1：systemd 白名单回填为换行分隔文本
    expect(fieldArea('systemd 展示白名单').value).toBe('sing-box');
  });

  test('id 在编辑模式不可改（改名会孤儿化关联记录）', async () => {
    render(
      <MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />
    );
    await waitFor(() => expect(fieldValue('机器 id')).toBe('kr'));
    expect(fieldInput('机器 id').disabled).toBe(true);
  });

  test('保存把原 id 当路径参数传（走 PUT 而非 POST）', async () => {
    const onSaved = vi.fn();
    render(<MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={onSaved} />);
    await waitFor(() => expect(fieldValue('显示名')).toBe('光帆 KR'));
    setField('显示名', '光帆 KR（改）');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    const [form, originalId] = mockedSave.mock.calls[0];
    expect(originalId).toBe('kr');
    expect(form.label).toBe('光帆 KR（改）');
    // 未触碰的敏感字段原样回传，不被清空
    expect(form.tailscale).toBe('100.64.0.50');
    expect(form.ssh_key).toBe('aws-kr.pem');
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  test('后端中文校验原因原样透出', async () => {
    mockedSave.mockRejectedValue({
      response: { data: { error: 'ssh 必须是 user@host 或 host 形式（首字符不能是连字符）' } },
    });
    render(
      <MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />
    );
    await waitFor(() => expect(fieldValue('显示名')).toBe('光帆 KR'));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() =>
      expect(mockedError).toHaveBeenCalledWith(expect.stringContaining('首字符不能是连字符'))
    );
  });

  test('读取失败时关闭抽屉而不是留个空表单', async () => {
    mockedGet.mockRejectedValue({ response: { data: { error: '机器不存在: nope' } } });
    const onOpenChange = vi.fn();
    render(
      <MachineSheet open mode="edit" machineId="nope" onOpenChange={onOpenChange} onSaved={vi.fn()} />
    );
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(mockedError).toHaveBeenCalledWith(expect.stringContaining('机器不存在'));
  });
});

describe('MachineSheet 家宽公网探测端口', () => {
  test('编辑时回填 public_probe.port，原样保存不丢', async () => {
    mockedGet.mockResolvedValue({ ...KR_FULL, public_probe: { type: 'tcp', port: 62037 } });
    render(
      <MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />
    );
    await waitFor(() => expect(fieldValue('家宽公网探测端口（tcp）')).toBe('62037'));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    expect(mockedSave.mock.calls[0][0].public_probe_port).toBe('62037');
  });

  test('未配置时为空；清空即不探（整份提交，空串交给 saveMachine 转 null）', async () => {
    render(
      <MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />
    );
    await waitFor(() => expect(fieldValue('显示名')).toBe('光帆 KR'));
    expect(fieldValue('家宽公网探测端口（tcp）')).toBe('');
    setField('家宽公网探测端口（tcp）', '62037');
    setField('家宽公网探测端口（tcp）', '');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    expect(mockedSave.mock.calls[0][0].public_probe_port).toBe('');
  });
});

describe('MachineSheet 添加模式', () => {
  test('不拉全量，id 可填，走 POST（originalId 为 undefined）', async () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    expect(mockedGet).not.toHaveBeenCalled();
    expect(fieldInput('机器 id').disabled).toBe(false);

    setField('机器 id', 'hk');
    setField('公网域名', 'hk.sunxc.eu.cc');
    setField('探活端口（tcp）', '62037');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    const [form, originalId] = mockedSave.mock.calls[0];
    expect(originalId).toBeUndefined();
    expect(form.id).toBe('hk');
    expect(form.public_host).toBe('hk.sunxc.eu.cc');
    expect(form.probe_port).toBe('62037');
  });

  test('角色与能力是按钮切换，默认 node + ssh', async () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    setField('机器 id', 'box');
    fireEvent.click(screen.getByRole('button', { name: '服务器' }));
    fireEvent.click(screen.getByRole('button', { name: 'Docker' }));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    const [form] = mockedSave.mock.calls[0];
    expect(form.role).toBe('server');
    expect(form.capabilities).toEqual(['ssh', 'docker']);
  });

  test('能力按钮可再点一次取消', async () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    setField('机器 id', 'box');
    fireEvent.click(screen.getByRole('button', { name: 'SSH' }));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    expect(mockedSave.mock.calls[0][0].capabilities).toEqual([]);
  });

  test('没有删除按钮（新建无可删对象）', () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.queryByRole('button', { name: '删除' })).toBeNull();
  });
});

describe('MachineSheet 删除', () => {
  /** 打开确认框，返回确认框内的「删除」按钮（抽屉里也有一个同名的，必须限定作用域）。 */
  async function openConfirm(): Promise<HTMLElement> {
    render(<MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(fieldValue('显示名')).toBe('光帆 KR'));
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    const dialog = await screen.findByRole('alertdialog');
    return within(dialog).getByRole('button', { name: '删除' });
  }

  test('先二次确认再删', async () => {
    const confirm = await openConfirm();
    expect(mockedDelete).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith('kr'));
    expect(mockedSuccess).toHaveBeenCalledWith('已删除 kr');
  });

  test('删除防呆的服务名列表照抄给用户', async () => {
    mockedDelete.mockRejectedValue({
      response: { data: { error: '机器 kr 上还有 2 个服务，请先迁移或删除：edge-kr、singbox-kr' } },
    });
    const confirm = await openConfirm();
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(mockedError).toHaveBeenCalledWith(expect.stringContaining('edge-kr、singbox-kr'))
    );
  });

  test('确认框写明探测时序与流量账本不会一起清理', async () => {
    await openConfirm();
    expect(screen.getByText(/探测时序与流量账本不会一起清理/)).toBeTruthy();
  });

  test('取消不删', async () => {
    await openConfirm();
    const dialog = screen.getByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));
    expect(mockedDelete).not.toHaveBeenCalled();
  });
});

describe('MachineSheet 红线提示（§5.3）', () => {
  test('密钥字段只收文件名，明确写清私钥不从这里上传', async () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByText(/私钥文件不从这里上传/)).toBeTruthy();
    // 没有 file input：私钥内容不经 HTTP
    expect(document.querySelector('input[type="file"]')).toBeNull();
  });

  test('renewal_ref 旁提示跨库 id 陷阱（§7.5）', () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByText(/console.db 不在 git 里/)).toBeTruthy();
  });

  test('关闭时不拉数据', () => {
    render(
      <MachineSheet open={false} mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />
    );
    expect(mockedGet).not.toHaveBeenCalled();
  });
});

describe('MachineSheet systemd 白名单（§4.1 / D5）', () => {
  const US_FULL: Machine = {
    id: 'us',
    label: 'Oracle',
    role: 'server',
    provider: 'Oracle Cloud',
    os: 'Ubuntu 24.04 ARM',
    region: '🇺🇸',
    beszel_name: 'US',
    capabilities: ['beszel', 'ssh'],
    systemd_units: ['sing-box'],
  };

  test('帮助文字明确只影响展示，不授予操作权限', () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByText(/只影响展示，不授予操作权限/)).toBeTruthy();
  });

  test('US 只读语义：白名单回填可编辑，保存只发展示白名单、不带动作字段', async () => {
    mockedGet.mockResolvedValue(US_FULL);
    render(<MachineSheet open mode="edit" machineId="us" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(fieldValue('显示名')).toBe('Oracle'));
    expect(fieldArea('systemd 展示白名单').value).toBe('sing-box');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    const [form, originalId] = mockedSave.mock.calls[0];
    expect(originalId).toBe('us');
    expect(form.systemd_units_text).toBe('sing-box');
    // 保存 payload 不含任何动作字段：US 是否能操作完全由后端 machine.actions 决定
    expect('actions' in form).toBe(false);
  });

  test('保存时 systemd 原文透传（trim/去重在 api.saveMachine 层，见 api.test）', async () => {
    render(<MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(fieldArea('systemd 展示白名单').value).toBe('sing-box'));
    fireEvent.change(fieldArea('systemd 展示白名单'), {
      target: { value: ' sing-box , docker.service\nsing-box ' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    expect(mockedSave.mock.calls[0][0].systemd_units_text).toBe(
      ' sing-box , docker.service\nsing-box '
    );
  });

  test('textarea 有 id 且 Label 用 htmlFor 关联（a11y，§13.7 gap#7）', () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    const area = document.getElementById('systemd-units');
    expect(area).not.toBeNull();
    expect(area?.tagName).toBe('TEXTAREA');
    const label = document.querySelector('label[for="systemd-units"]');
    expect(label).not.toBeNull();
    expect(label?.textContent).toContain('systemd 展示白名单');
  });

  test('从编辑态切新增会复位 loading，不卡「读取中…」（§13.7 gap#6）', async () => {
    // getMachineForEdit 永不 resolve，编辑态停留在 loading
    mockedGet.mockImplementation(() => new Promise(() => {}));
    const { rerender } = render(
      <MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />
    );
    await waitFor(() => expect(screen.getByText('读取中…')).toBeTruthy());

    // 切到新增：早期 return 分支必须 setLoading(false)
    rerender(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.queryByText('读取中…')).toBeNull());
    expect(fieldInput('机器 id').disabled).toBe(false);
  });
});

describe('L3 机器预设', () => {
  test('新增时套用 node·sing-box 预设填好角色 / 能力 / 探活端口 / systemd 白名单', async () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'node · sing-box' }));
    await waitFor(() => expect(fieldValue('探活端口（tcp）')).toBe('62037'));
    expect(fieldArea('systemd 展示白名单').value).toBe('sing-box');

    setField('机器 id', 'hk2');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    const [form] = mockedSave.mock.calls[mockedSave.mock.calls.length - 1];
    expect(form.role).toBe('node');
    expect(form.capabilities).toEqual(['beszel', 'ssh']);
  });

  test('预设不碰 id / 地址 / 凭据（那些填错会静默指向别的机器）', async () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    setField('机器 id', 'hk');
    fireEvent.click(screen.getByRole('button', { name: 'server · Docker' }));
    await waitFor(() => expect(fieldValue('探活端口（tcp）')).toBe('22'));
    expect(fieldValue('机器 id')).toBe('hk');
    expect(fieldValue('Tailscale IP')).toBe('');
  });

  test('手改预设管辖的字段后撤掉高亮（按钮不再声称「就是这个预设」）', async () => {
    render(<MachineSheet open mode="add" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    const button = screen.getByRole('button', { name: 'server · Docker' });
    fireEvent.click(button);
    await waitFor(() => expect(button.className).toContain('bg-primary'));
    setField('探活端口（tcp）', '2222');
    await waitFor(() => expect(button.className).not.toContain('bg-primary'));
  });

  test('编辑既有机器时不给预设（按一下会静默改掉动作权限边界）', async () => {
    render(
      <MachineSheet open mode="edit" machineId="kr" onOpenChange={vi.fn()} onSaved={vi.fn()} />
    );
    await waitFor(() => expect(mockedGet).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'server · Docker' })).toBeNull();
  });
});

describe('旧 Hub 退役后的 systemd 白名单', () => {
  test('仍保留手工编辑，不展示在线 unit 候选', async () => {
    // 组件只接受 open/mode/machineId：编辑态按 machineId 拉 ?full=1，
    // 已发现的 unit 即便随详情返回，也不能变成可点的候选按钮。
    mockedGet.mockResolvedValue({
      ...KR_FULL,
      id: 'us',
      label: 'US',
      beszel_name: 'US',
      capabilities: ['beszel', 'ssh'],
      discovered_units: ['sing-box', 'cron'],
    });
    render(
      <MachineSheet open mode="edit" machineId="us" onOpenChange={() => {}} onSaved={() => {}} />
    );
    await waitFor(() => expect(screen.getByLabelText('systemd 展示白名单')).toBeTruthy());
    expect(screen.queryByRole('button', { name: '+ cron' })).toBeNull();
  });
});

describe('MachineSheet 服务升级闸（2026-10-22）', () => {
  const NAS_UPGRADE_ENABLED: Machine = {
    id: 'nas',
    label: 'NAS',
    role: 'server',
    beszel_name: 'NAS',
    capabilities: ['docker', 'beszel', 'ssh'],
    upgrade: {
      enabled: true,
      proxy_env: { HTTPS_PROXY: 'http://192.168.50.101:7890' },
    },
  };

  test('upgrade.enabled=true 时按钮显示「允许升级」并回填 proxy_env 文本', async () => {
    mockedGet.mockResolvedValue(NAS_UPGRADE_ENABLED);
    render(<MachineSheet open mode="edit" machineId="nas" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(fieldValue('显示名')).toBe('NAS'));
    const btn = screen.getByRole('button', { name: '允许升级' });
    expect(btn.className).toContain('bg-primary');
    expect(fieldArea('升级代理 env（可选）').value).toBe(
      'HTTPS_PROXY=http://192.168.50.101:7890'
    );
  });

  test('upgrade.enabled=false（或缺失）时按钮显示「禁止升级」', async () => {
    mockedGet.mockResolvedValue({
      ...NAS_UPGRADE_ENABLED,
      upgrade: { enabled: false },
    });
    render(<MachineSheet open mode="edit" machineId="nas" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(fieldValue('显示名')).toBe('NAS'));
    expect(screen.getByRole('button', { name: '禁止升级' }).className).not.toContain('bg-primary');
    // proxy_env 缺失时空文本
    expect(fieldArea('升级代理 env（可选）').value).toBe('');
  });

  test('点「禁止升级」切到「允许升级」并保存升级 enabled', async () => {
    mockedGet.mockResolvedValue({
      ...NAS_UPGRADE_ENABLED,
      upgrade: { enabled: false },
    });
    render(<MachineSheet open mode="edit" machineId="nas" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(fieldValue('显示名')).toBe('NAS'));
    fireEvent.click(screen.getByRole('button', { name: '禁止升级' }));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    const [form] = mockedSave.mock.calls[mockedSave.mock.calls.length - 1];
    expect(form.upgrade_enabled).toBe(true);
  });

  test('禁止升级时 proxy env textarea 禁用（避免误填后丢失语义）', async () => {
    mockedGet.mockResolvedValue({
      ...NAS_UPGRADE_ENABLED,
      upgrade: { enabled: false },
    });
    render(<MachineSheet open mode="edit" machineId="nas" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(fieldValue('显示名')).toBe('NAS'));
    expect(fieldArea('升级代理 env（可选）').disabled).toBe(true);
  });

  test('保存时 proxy_env 文本框原文透传（清洗在 api.saveMachine 层）', async () => {
    mockedGet.mockResolvedValue(NAS_UPGRADE_ENABLED);
    render(<MachineSheet open mode="edit" machineId="nas" onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(fieldValue('显示名')).toBe('NAS'));
    fireEvent.change(fieldArea('升级代理 env（可选）'), {
      target: { value: 'HTTPS_PROXY=http://p:1\nNO_PROXY=localhost' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(mockedSave).toHaveBeenCalled());
    const [form] = mockedSave.mock.calls[mockedSave.mock.calls.length - 1];
    expect(form.upgrade_proxy_env_text).toBe('HTTPS_PROXY=http://p:1\nNO_PROXY=localhost');
  });
});
