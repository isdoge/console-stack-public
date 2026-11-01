import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Textarea } from '@/components/ui/textarea';
import type { MachineFormValues, MachineRole } from '@/lib/types';
import { deleteMachine, getMachineForEdit, saveMachine,
  apiErrorMessage, formatProxyEnvText,
} from '@/lib/api';

const CAPABILITIES: Array<[string, string]> = [
  ['docker', 'Docker'],
  ['ssh', 'SSH'],
];

const EMPTY: MachineFormValues = {
  id: '',
  label: '',
  role: 'node',
  provider: '',
  os: '',
  region: '',
  public_host: '',
  tailscale: '',
  ssh: '',
  ssh_key: '',
  beszel_name: '',
  capabilities: ['ssh'],
  probe_port: '',
  public_probe_port: '',
  traffic_quota_gb: '',
  traffic_nic: '',
  renewal_ref: '',
  systemd_units_text: '',
  upgrade_enabled: false,
  upgrade_proxy_env_text: '',
};

/**
 * L3 机器预设（设计 §5.6）：**只填 role / capabilities / probe 三类常用值**，
 * 其余（id、地址、凭据、配额）永远由人填，保存前每一项都还能改。
 *
 * 它解决的是「新增一台机器要先想清楚 capabilities 该勾哪几个」的认知成本，
 * 不是替人做决定 —— 所以刻意不预填 ssh / tailscale / beszel_name：
 * 那些填错了会静默指向错误的机器。
 */
export const MACHINE_PRESETS: {
  key: string;
  label: string;
  hint: string;
  values: Pick<MachineFormValues, 'role' | 'capabilities' | 'probe_port' | 'systemd_units_text'>;
}[] = [
  {
    key: 'server-docker',
    label: 'server · Docker',
    hint: '跑 Docker 的服务器（nas / txy / aly 这一类）：容器动作 + SSH，探活打 22。',
    values: {
      role: 'server',
      capabilities: ['docker', 'ssh'],
      probe_port: '22',
      systemd_units_text: '',
    },
  },
  {
    key: 'server-agent-only',
    label: 'server · 无 Docker',
    hint: '有 SSH 但没有 Docker（us 这一类）：不给容器动作，探活打 22。',
    values: {
      role: 'server',
      capabilities: ['beszel', 'ssh'],
      probe_port: '22',
      systemd_units_text: '',
    },
  },
  {
    key: 'node-singbox',
    label: 'node · sing-box',
    hint: '纯代理节点（光帆 kr / sg 这一类）：探活打 sing-box 端口，systemd 白名单预置 sing-box。',
    values: {
      role: 'node',
      capabilities: ['beszel', 'ssh'],
      probe_port: '62037',
      systemd_units_text: 'sing-box',
    },
  },
];

type Mode = 'edit' | 'add';

interface Props {
  open: boolean;
  mode: Mode;
  /** 编辑模式下要改的机器 id；表单值另外走 ?full=1 拉全量 */
  machineId?: string | null;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}

export function MachineSheet({ open, mode, machineId, onOpenChange, onSaved }: Props) {
  const [form, setForm] = useState<MachineFormValues>(EMPTY);
  const [activePreset, setActivePreset] = useState<string>('');
  /** L2：旧 Hub 已发现的全部 unit 名（`?full=1` 带回）；拿不到就是空数组 */
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (!open) return;
    setActivePreset('');
    if (mode !== 'edit' || !machineId) {
      setForm(EMPTY);
      // 从编辑态切到新增/关闭再开时，上一次的 loading 必须复位，否则卡在「读取中…」
      setLoading(false);
      return;
    }
    // 编辑必须读 ?full=1：列表接口不含 tailscale/ssh/ssh_key，
    // 拿列表数据填表单会在保存时把这三项清空。
    let alive = true;
    setLoading(true);
    getMachineForEdit(machineId)
      .then((m) => {
        if (!alive) return;
        setForm({
          id: m.id ?? '',
          label: m.label ?? '',
          role: (m.role === 'server' ? 'server' : 'node') as MachineFormValues['role'],
          provider: m.provider ?? '',
          os: m.os ?? '',
          region: m.region ?? '',
          public_host: m.public_host ?? '',
          tailscale: m.tailscale ?? '',
          ssh: m.ssh ?? '',
          ssh_key: m.ssh_key ?? '',
          beszel_name: m.beszel_name ?? '',
          capabilities: m.capabilities ?? [],
          probe_port: m.probe?.port ? String(m.probe.port) : '',
          public_probe_port: m.public_probe?.port ? String(m.public_probe.port) : '',
          traffic_quota_gb:
            m.traffic_quota_gb === undefined || m.traffic_quota_gb === null
              ? ''
              : String(m.traffic_quota_gb),
          traffic_nic: m.traffic_nic ?? '',
          renewal_ref: m.renewal_ref ?? '',
          // §4.1：白名单只决定显示什么，不授予操作权限
          systemd_units_text: (m.systemd_units || []).join('\n'),
          upgrade_enabled: m.upgrade?.enabled ?? false,
          upgrade_proxy_env_text: formatProxyEnvText(m.upgrade?.proxy_env),
        });
      })
      .catch((err: unknown) => {
        toast.error(`读取失败: ${apiErrorMessage(err)}`);
        onOpenChange(false);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // onOpenChange 只在卸载分支用，不进依赖以免每次重渲染重拉
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, machineId]);

  /** L3：套用预设。只覆盖预设声明的那几项，已填的 id / 地址 / 凭据一律不动。 */
  const applyPreset = (key: string) => {
    const preset = MACHINE_PRESETS.find((item) => item.key === key);
    if (!preset) return;
    setActivePreset(key);
    setForm((prev) => ({ ...prev, ...preset.values }));
  };

  // 手改了预设管辖的字段就撤掉高亮：按钮 variant 表达的是「当前就是这个预设」，
  // 改完还亮着等于说谎。
  const PRESET_FIELDS: ReadonlySet<keyof MachineFormValues> = new Set([
    'role',
    'capabilities',
    'probe_port',
    'systemd_units_text',
  ]);

  const setField = <K extends keyof MachineFormValues>(key: K, value: MachineFormValues[K]) => {
    if (PRESET_FIELDS.has(key)) setActivePreset('');
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  /** 白名单文本框现有条目（逗号/换行分隔，与 saveMachine 的拆分口径一致）。 */




  const toggleCapability = (cap: string) => {
    setActivePreset('');
    setForm((prev) => ({
      ...prev,
      capabilities: prev.capabilities.includes(cap)
        ? prev.capabilities.filter((c) => c !== cap)
        : [...prev.capabilities, cap],
    }));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await saveMachine(form, mode === 'edit' ? machineId || undefined : undefined);
      toast.success(mode === 'edit' ? `已保存 ${form.id}` : `已添加 ${form.id}`);
      onOpenChange(false);
      onSaved();
    } catch (err: unknown) {
      // 后端把校验原因写在 error 里（中文），原样透出比「保存失败」有用
      toast.error(`保存失败: ${apiErrorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!machineId) return;
    try {
      await deleteMachine(machineId);
      toast.success(`已删除 ${machineId}`);
      setConfirmDelete(false);
      onOpenChange(false);
      onSaved();
    } catch (err: unknown) {
      // 机器上还有服务时后端 400 并列出服务名，照抄给用户
      toast.error(`删除失败: ${apiErrorMessage(err)}`);
    }
  };

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent className="w-full sm:max-w-lg overflow-y-auto">
          <SheetHeader>
            <SheetTitle>{mode === 'edit' ? `编辑机器 · ${machineId}` : '添加机器'}</SheetTitle>
            <SheetDescription>
              写回 machines.yaml。id 是服务台账、探测时序和流量账本的关联键，保存后不可改名。
            </SheetDescription>
          </SheetHeader>

          {loading ? (
            <p className="mt-6 text-sm text-muted-foreground">读取中…</p>
          ) : (
            <div className="mt-6 space-y-4">
              {/* L3：预设只在「添加」时给。编辑既有机器时按一下会静默改掉
                  capabilities（= 改动作权限边界），风险不对等，所以不放。 */}
              {mode === 'add' && (
                <Field label="预设">
                  <div className="space-y-1.5">
                    <div className="flex flex-wrap gap-1.5">
                      {MACHINE_PRESETS.map((preset) => (
                        <Button
                          key={preset.key}
                          type="button"
                          size="sm"
                          variant={activePreset === preset.key ? 'default' : 'outline'}
                          onClick={() => applyPreset(preset.key)}
                        >
                          {preset.label}
                        </Button>
                      ))}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {MACHINE_PRESETS.find((p) => p.key === activePreset)?.hint ??
                        '选一个预设填好角色 / 能力 / 探活端口，其余仍需自己填，保存前都能改。'}
                    </p>
                  </div>
                </Field>
              )}
              <div className="grid grid-cols-2 gap-3">
                <Field label="机器 id" required>
                  <Input
                    value={form.id}
                    onChange={(e) => setField('id', e.target.value)}
                    disabled={mode === 'edit'}
                    placeholder="kr / hk"
                    className="font-mono"
                  />
                </Field>
                <Field label="显示名">
                  <Input
                    value={form.label}
                    onChange={(e) => setField('label', e.target.value)}
                    placeholder="缺省用大写 id"
                  />
                </Field>
              </div>

              <Field label="角色" required>
                <div className="flex gap-2">
                  {([
                    ['server', '服务器'],
                    ['node', '节点'],
                  ] as Array<[MachineRole, string]>).map(([value, label]) => (
                    <Button
                      key={value}
                      type="button"
                      size="sm"
                      variant={form.role === value ? 'default' : 'outline'}
                      onClick={() => setField('role', value as MachineFormValues['role'])}
                    >
                      {label}
                    </Button>
                  ))}
                </div>
              </Field>

              <div className="grid grid-cols-2 gap-3">
                <Field label="服务商">
                  <Input
                    value={form.provider}
                    onChange={(e) => setField('provider', e.target.value)}
                    placeholder="如 AWS Lightsail 韩国"
                  />
                </Field>
                <Field label="系统">
                  <Input
                    value={form.os}
                    onChange={(e) => setField('os', e.target.value)}
                    placeholder="如 Ubuntu 24.04"
                  />
                </Field>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <Field label="地区旗帜">
                  <Input
                    value={form.region}
                    onChange={(e) => setField('region', e.target.value)}
                    placeholder="🇰🇷"
                  />
                </Field>
                <Field label="归档 Hub 名称">
                  <Input
                    value={form.beszel_name}
                    onChange={(e) => setField('beszel_name', e.target.value)}
                    placeholder="缺省用大写 id"
                  />
                </Field>
              </div>

              <Field label="能力">
                <div className="flex gap-2">
                  {CAPABILITIES.map(([value, label]) => (
                    <Button
                      key={value}
                      type="button"
                      size="sm"
                      variant={form.capabilities.includes(value) ? 'default' : 'outline'}
                      onClick={() => toggleCapability(value)}
                    >
                      {label}
                    </Button>
                  ))}
                </div>
              </Field>

              <Separator />

              <Field label="公网域名">
                <Input
                  value={form.public_host}
                  onChange={(e) => setField('public_host', e.target.value)}
                  placeholder="kr.example.com（只填主机名，不带协议）"
                  className="font-mono"
                />
              </Field>
              <Field label="Tailscale IP">
                <Input
                  value={form.tailscale}
                  onChange={(e) => setField('tailscale', e.target.value)}
                  placeholder="100.x.x.x"
                  className="font-mono"
                />
              </Field>
              <Field label="SSH 目标">
                <Input
                  value={form.ssh}
                  onChange={(e) => setField('ssh', e.target.value)}
                  placeholder="root@100.x.x.x；留空表示本机执行"
                  className="font-mono"
                />
              </Field>
              <Field label="SSH 密钥文件名">
                <Input
                  value={form.ssh_key}
                  onChange={(e) => setField('ssh_key', e.target.value)}
                  placeholder="aws-kr.pem"
                  className="font-mono"
                />
                <p className="text-xs text-muted-foreground">
                  只填 .ssh-console/ 里的文件名。私钥文件不从这里上传，由人放进该目录。
                </p>
              </Field>
              <Field label="探活端口（tcp）">
                <Input
                  value={form.probe_port}
                  onChange={(e) => setField('probe_port', e.target.value)}
                  placeholder="留空 = 不探活；server 一般 22"
                  className="font-mono"
                />
              </Field>
              <Field label="家宽公网探测端口（tcp）">
                <Input
                  value={form.public_probe_port}
                  onChange={(e) => setField('public_probe_port', e.target.value)}
                  placeholder="留空 = 不探；代理节点一般 62037"
                  className="font-mono"
                />
                <p className="text-xs text-muted-foreground">
                  从 NAS 经家宽直连上面的公网域名，看代理端口通不通；与探活（走 Tailscale）互不影响，需先填公网域名。
                </p>
              </Field>

              <Separator />

              <div className="grid grid-cols-2 gap-3">
                <Field label="月流量配额 (GB)">
                  <Input
                    value={form.traffic_quota_gb}
                    onChange={(e) => setField('traffic_quota_gb', e.target.value)}
                    placeholder="留空 = 不限"
                    className="font-mono"
                  />
                </Field>
                <Field label="计费网卡">
                  <Input
                    value={form.traffic_nic}
                    onChange={(e) => setField('traffic_nic', e.target.value)}
                    placeholder="留空 = 自动挑选"
                    className="font-mono"
                  />
                </Field>
              </div>
              <Field label="续费条目 id">
                <Input
                  value={form.renewal_ref}
                  onChange={(e) => setField('renewal_ref', e.target.value)}
                  placeholder="到期提醒里的 id"
                  className="font-mono"
                />
                <p className="text-xs text-muted-foreground">
                  指向到期提醒（console.db）的 id，而 console.db 不在 git 里；换机重建库后要重新对齐，
                  否则价格信息会静默消失。
                </p>
              </Field>

              <Separator />

              <Field label="systemd 展示白名单" htmlFor="systemd-units">
                <Textarea
                  id="systemd-units"
                  value={form.systemd_units_text}
                  onChange={(e) => setField('systemd_units_text', e.target.value)}
                  placeholder={'sing-box\ndocker.service'}
                  className="font-mono"
                  rows={3}
                />
                <p className="text-xs text-muted-foreground">
                  逗号或换行分隔；只影响展示，不授予操作权限（重启等动作仍走机器能力白名单）。
                </p>
                {/* 旧 Hub 发现源已退役；白名单仍可手工维护。 */}
              </Field>

              <Separator />

              <Field label="服务升级">
                <div className="flex gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant={form.upgrade_enabled ? 'default' : 'outline'}
                    onClick={() => setField('upgrade_enabled', !form.upgrade_enabled)}
                  >
                    {form.upgrade_enabled ? '允许升级' : '禁止升级'}
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  允许后，这台机器上登记了 compose_dir 的服务会在服务卡出现 upgrade 按钮；
                  命令走 SSH 到本机跑 docker compose pull &amp;&amp; up -d。
                </p>
              </Field>

              <Field label="升级代理 env（可选）" htmlFor="upgrade-proxy-env">
                <Textarea
                  id="upgrade-proxy-env"
                  value={form.upgrade_proxy_env_text}
                  onChange={(e) => setField('upgrade_proxy_env_text', e.target.value)}
                  placeholder={'HTTPS_PROXY=http://192.168.50.101:7890\nNO_PROXY=localhost,127.0.0.1'}
                  className="font-mono"
                  rows={3}
                  disabled={!form.upgrade_enabled}
                />
                <p className="text-xs text-muted-foreground">
                  一行一个 <code>KEY=value</code>；只允许 HTTP(S)_PROXY / NO_PROXY 等白名单键，
                  值限字母数字与常用代理符号。NAS 守护进程已全局走 mihomo，一般留空。
                </p>
              </Field>
            </div>
          )}

          <SheetFooter className="mt-6 flex-row items-center gap-2">
            {mode === 'edit' && machineId && (
              <Button
                type="button"
                variant="destructive"
                onClick={() => setConfirmDelete(true)}
                disabled={saving || loading}
              >
                删除
              </Button>
            )}
            <div className="ml-auto flex gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={saving}
              >
                取消
              </Button>
              <Button type="button" onClick={handleSave} disabled={saving || loading}>
                {saving ? '保存中…' : '保存'}
              </Button>
            </div>
          </SheetFooter>
        </SheetContent>
      </Sheet>

      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除机器？</AlertDialogTitle>
            <AlertDialogDescription>
              将从 machines.yaml 删除 <span className="font-mono">{machineId}</span>
              。机器上还有服务时后端会拒绝。已有的探测时序与流量账本不会一起清理。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete}>删除</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function Field({
  label,
  required,
  htmlFor,
  children,
}: {
  label: string;
  required?: boolean;
  htmlFor?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={htmlFor}>
        {label}
        {required && <span className="ml-1 text-destructive">*</span>}
      </Label>
      {children}
    </div>
  );
}
