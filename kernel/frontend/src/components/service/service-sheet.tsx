import { useEffect, useRef, useState } from 'react';
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
import type {
  HealthProbeMode,
  HealthProbeSpec,
  Machine,
  Service,
  ServiceFormValues,
  ServiceAction,
} from '@/lib/types';
import { allowedActions } from './service-actions';
import { deleteService, extractPathSuffix, saveService, suggestAccess,
  apiErrorMessage,
} from '@/lib/api';

const ACTION_LABELS: Record<ServiceAction, string> = { start: '启动', stop: '停止', restart: '重启', logs: '日志', upgrade: '升级' };
const EMPTY: ServiceFormValues = {
  type: 'docker',
  systemd_unit: '',
  systemd_scope: 'system',
  actions: ['start', 'stop', 'restart', 'logs'],
  name: '',
  machine: 'nas',
  port: '',
  purpose: '',
  priority: 'medium',
  note: '',
  deploy: '',
  home: '',
  tailscale: '',
  domain: '',
  repo_url: '',
  version_enabled: false,
  track_upstream: true,
  health_probe_mode: 'default',
  health_probe_target: 'domain',
  health_probe_status: '200',
  health_probe_latency: '',
  health_probe_body: '',
  tags: [],
  container: '',
  compose_dir: '',
  compose_file: '',
};

const HEALTH_PROBE_TARGETS: HealthProbeSpec['target'][] = ['domain', 'tailscale', 'home'];
const TARGET_LABELS: Record<HealthProbeSpec['target'], string> = {
  domain: '公网域名',
  tailscale: 'Tailscale',
  home: '家庭内网',
};

type HealthProbeFields = Pick<
  ServiceFormValues,
  | 'health_probe_mode'
  | 'health_probe_target'
  | 'health_probe_status'
  | 'health_probe_latency'
  | 'health_probe_body'
>;

const PROBE_FIELD_DEFAULTS: Omit<HealthProbeFields, 'health_probe_mode'> = {
  health_probe_target: EMPTY.health_probe_target,
  health_probe_status: EMPTY.health_probe_status,
  health_probe_latency: '',
  health_probe_body: '',
};

/**
 * 台账里的 `health_probe`（三态）→ 表单字段。
 *
 * 坏对象按「默认推断」回填，与 `probe_cache._semantic_probe` 的宽容口径一致：
 * **配错不等于关掉** —— 把写坏的 YAML 回填成「关闭探测」会静默改变行为。
 */
export function formFromHealthProbe(raw: Service['health_probe']): HealthProbeFields {
  if (raw === false) {
    return { ...PROBE_FIELD_DEFAULTS, health_probe_mode: 'off' };
  }
  if (raw && typeof raw === 'object' && raw.type === 'http') {
    return {
      health_probe_mode: 'http',
      // 非法 target（手写 YAML）退回 domain，否则三个按钮一个都不高亮
      health_probe_target: HEALTH_PROBE_TARGETS.includes(raw.target)
        ? raw.target
        : 'domain',
      health_probe_status: raw.expected_status == null ? '' : String(raw.expected_status),
      health_probe_latency: raw.max_latency_ms == null ? '' : String(raw.max_latency_ms),
      health_probe_body: raw.body_contains ?? '',
    };
  }
  return { ...PROBE_FIELD_DEFAULTS, health_probe_mode: 'default' };
}

const HEALTH_PROBE_MODES: { value: HealthProbeMode; label: string; hint: string }[] = [
  {
    value: 'default',
    label: '默认',
    hint: '有端口就打 TCP，否则打域名。端口通即算健康——业务返回 500 也看不出来。',
  },
  {
    value: 'http',
    label: 'HTTP 语义',
    hint: '按状态码 / 耗时 / 响应内容判断，能抓到「端口开着但业务坏了」。',
  },
  {
    value: 'off',
    label: '关闭',
    hint: '不探测、不进状态统计。给内部数据库、只绑回环的端口、主动出站 agent 用。',
  },
];

type Mode = 'edit' | 'add';

interface Props {
  open: boolean;
  mode: Mode;
  service?: Service | null;
  machines: Machine[];
  defaultMachineId?: string;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}

export function ServiceSheet({
  open,
  mode,
  service,
  machines,
  defaultMachineId,
  onOpenChange,
  onSaved,
}: Props) {
  const [form, setForm] = useState<ServiceFormValues>(EMPTY);
  const [probe, setProbe] = useState<Record<string, { configured: boolean; ok: boolean | null; msg?: string }> | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const initializingSuggest = useRef(false);
  const initialSuggestBasis = useRef({ machine: EMPTY.machine, port: EMPTY.port });
  // 供 suggest effect 读取最新地址而不进入依赖数组
  const accessRef = useRef(form);
  accessRef.current = form;

  useEffect(() => {
    if (!open) return;
    setProbe(null);
    let nextForm: ServiceFormValues;
    if (mode === 'edit' && service) {
      nextForm = {
        type: service.type || 'docker',
        systemd_unit: service.systemd_unit || '',
        systemd_scope: service.systemd_scope || 'system',
        actions: [...allowedActions(service)],
        name: service.name,
        machine: service.machine,
        port: String(service.port ?? ''),
        purpose: service.purpose ?? '',
        priority: service.priority ?? 'medium',
        note: service.note ?? '',
        deploy: service.deploy ?? '',
        home: service.access?.home ?? '',
        tailscale: service.access?.tailscale ?? '',
        domain: service.access?.domain ?? '',
        repo_url: service.version?.repo_url ?? '',
        version_enabled: service.version?.enabled ?? Boolean(service.version?.repo_url),
        track_upstream: service.version?.track_upstream !== false,
        ...formFromHealthProbe(service.health_probe),
        tags: service.tags ?? [],
        container: service.container ?? '',
        compose_dir: service.compose_dir ?? '',
        compose_file: service.compose_file ?? '',
      };
    } else {
      nextForm = { ...EMPTY, machine: defaultMachineId || EMPTY.machine };
    }
    initializingSuggest.current = true;
    initialSuggestBasis.current = {
      machine: nextForm.machine,
      port: nextForm.port,
    };
    setForm(nextForm);
  }, [open, mode, service, defaultMachineId]);

  useEffect(() => {
    if (!open) return;
    if (initializingSuggest.current) {
      const basis = initialSuggestBasis.current;
      if (form.machine === basis.machine && form.port === basis.port) {
        initializingSuggest.current = false;
      }
      return;
    }
    if (!form.port) return;
    // suggest 触发只看 machine/port；地址取最新值（ref 避免 setForm 回写引发 effect 重跑）
    const suffix = extractPathSuffix(accessRef.current.home) || extractPathSuffix(accessRef.current.tailscale);
    const timer = setTimeout(() => {
      suggestAccess(form.machine, form.port, suffix)
        .then((res) => {
          setForm((prev) => ({
            ...prev,
            home: res.home || prev.home,
            tailscale: res.tailscale || prev.tailscale,
          }));
        })
        .catch(() => {});
    }, 350);
    return () => clearTimeout(timer);
  }, [form.machine, form.port, open]);

  const handleSave = async () => {
    // 前端先拦一道必填（后端 validate_identifier 仍兜底）
    if (!form.name.trim() || !form.machine.trim()) {
      toast.error('服务名称和所属主机为必填项');
      return;
    }
    setSaving(true);
    try {
      const res = await saveService(form, service?.name);
      setProbe(res.access_probe || null);
      toast.success('已保存并触发探测');
      onSaved();
    } catch (err: unknown) {
      toast.error(`保存失败: ${apiErrorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!service) return;
    try {
      await deleteService(service.name);
      toast.success(`已删除 ${service.name}`);
      setConfirmDelete(false);
      onOpenChange(false);
      onSaved();
    } catch (err: unknown) {
      toast.error(`删除失败: ${apiErrorMessage(err)}`);
    }
  };

  const setField = <K extends keyof ServiceFormValues>(key: K, value: ServiceFormValues[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const machineOptions = machines.map((machine) => ({
    id: machine.id,
    label: machine.label || machine.id,
  }));
  if (form.machine && !machineOptions.some((machine) => machine.id === form.machine)) {
    machineOptions.push({ id: form.machine, label: form.machine });
  }

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent className="w-full sm:max-w-lg overflow-y-auto">
          <SheetHeader>
            <SheetTitle>{mode === 'edit' ? `编辑服务 · ${service?.name}` : '添加服务'}</SheetTitle>
            <SheetDescription>
              修改主机 / 端口会自动填充家庭 & Tailscale 地址；保存时后端会探测连通性。
            </SheetDescription>
          </SheetHeader>

          <div className="mt-6 space-y-4">
            <Field label="服务名称" required>
              <Input value={form.name} onChange={(e) => setField('name', e.target.value)} />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="所属主机">
                <select
                  aria-label="所属主机"
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  value={form.machine}
                  onChange={(e) => setField('machine', e.target.value)}
                >
                  {machineOptions.map((machine) => (
                    <option key={machine.id} value={machine.id}>
                      {machine.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="端口">
                <Input value={form.port} onChange={(e) => setField('port', e.target.value)} />
              </Field>
            </div>
            <Field label="容器名" hint="与台账名不同时填（如 axonhub → 1panel-axonhub）；留空用服务名">
              <Input value={form.container ?? ''} onChange={(e) => setField('container', e.target.value)} />
            </Field>
            {(form.type === 'docker' || form.type === 'docker-compose') && <>
              <Field label="compose 目录" hint="升级按钮所需；如 /opt/ignis；留空则该服务不可点升级">
                <Input value={form.compose_dir ?? ''} onChange={(e) => setField('compose_dir', e.target.value)} />
              </Field>
              <Field label="compose 文件名" hint="缺省 compose.yaml；老项目可填 docker-compose.yml">
                <Input value={form.compose_file ?? ''} onChange={(e) => setField('compose_file', e.target.value)} />
              </Field>
            </>}
            <Field label="用途">
              <Input value={form.purpose} onChange={(e) => setField('purpose', e.target.value)} />
            </Field>
            <Field label="优先级">
              <div className="flex gap-2">
                {([
                  ['high', '高'],
                  ['medium', '中'],
                  ['low', '低'],
                ] as const).map(([value, label]) => (
                  <Button
                    key={value}
                    type="button"
                    size="sm"
                    variant={form.priority === value ? 'default' : 'outline'}
                    onClick={() => setField('priority', value)}
                  >
                    {label}
                  </Button>
                ))}
              </div>
            </Field>
            <Field label="管理方式">
              <select aria-label="管理方式" value={form.type} className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                onChange={(e) => setForm((prev) => ({ ...prev, type: e.target.value,
                  systemd_unit: '', actions: e.target.value === 'process' ? [] : ['start', 'stop', 'restart', 'logs'] }))}>
                <option value="docker">Docker 容器</option>
                <option value="docker-compose">Docker Compose 容器</option>
                <option value="process">主机进程 / systemd</option>
              </select>
            </Field>
            {form.type === 'process' && <>
              <Field label="systemd 服务单元">
                <Input aria-label="systemd 服务单元" value={form.systemd_unit || ''}
                  placeholder="如 token-scope.service；无管理单元可留空"
                  onChange={(e) => setField('systemd_unit', e.target.value)} />
              </Field>
              <Field label="systemd 范围">
                <select aria-label="systemd 范围" value={form.systemd_scope} className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                  onChange={(e) => setField('systemd_scope', e.target.value as 'system' | 'user')}>
                  <option value="system">系统服务</option><option value="user">用户服务</option>
                </select>
              </Field>
            </>}
            <Field label="允许的操作">
              <div className="flex flex-wrap gap-3">
                {(Object.keys(ACTION_LABELS) as ServiceAction[]).map((action) => <label key={action} className="flex items-center gap-1.5 text-sm">
                  <input type="checkbox" aria-label={`允许${ACTION_LABELS[action]}`}
                    checked={(form.actions || []).includes(action)}
                    disabled={form.type === 'process' && !form.systemd_unit}
                    onChange={(e) => setField('actions', e.target.checked
                      ? [...(form.actions || []), action] : (form.actions || []).filter((item) => item !== action))} />
                  {ACTION_LABELS[action]}
                </label>)}
              </div>
            </Field>
            <Field label="部署环境">
              <Input
                value={form.deploy}
                onChange={(e) => setField('deploy', e.target.value)}
                placeholder="如 Docker / 1Panel + Docker"
              />
            </Field>
            <Field label="备注">
              <Input value={form.note} onChange={(e) => setField('note', e.target.value)} />
            </Field>
            <Field label="仓库上游 (GitHub)">
              <Input
                value={form.repo_url}
                onChange={(e) => {
                  const repoUrl = e.target.value;
                  setForm((prev) => ({
                    ...prev,
                    repo_url: repoUrl,
                    version_enabled: repoUrl ? true : prev.version_enabled,
                  }));
                }}
              />
            </Field>
            <div className="flex items-center gap-2">
              <input
                id="service-version-enabled"
                type="checkbox"
                checked={form.version_enabled}
                onChange={(e) => setField('version_enabled', e.target.checked)}
                className="h-4 w-4 rounded border"
              />
              <Label htmlFor="service-version-enabled">
                启用版本追踪（无 GitHub 仓库也可记录本机版本）
              </Label>
            </div>
            <div className="flex items-center gap-2">
              <input
                id="service-track-upstream"
                type="checkbox"
                checked={form.track_upstream}
                onChange={(e) => setField('track_upstream', e.target.checked)}
                className="h-4 w-4 rounded border"
              />
              <Label htmlFor="service-track-upstream">
                跟踪上游更新（关闭后仍查版本但不提示有新版本）
              </Label>
            </div>
            <Field label="健康检查">
              <div className="space-y-2">
                <div className="flex flex-wrap gap-1">
                  {HEALTH_PROBE_MODES.map((option) => (
                    <Button
                      key={option.value}
                      type="button"
                      variant={form.health_probe_mode === option.value ? 'secondary' : 'ghost'}
                      size="sm"
                      className="h-7 px-2 text-xs"
                      onClick={() => setField('health_probe_mode', option.value)}
                      aria-pressed={form.health_probe_mode === option.value}
                    >
                      {option.label}
                    </Button>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  {HEALTH_PROBE_MODES.find((o) => o.value === form.health_probe_mode)?.hint}
                </p>
                {form.health_probe_mode === 'http' && (
                  <div className="space-y-2 rounded-md border border-border/60 p-2.5">
                    <div className="flex flex-wrap items-center gap-1">
                      <span className="text-xs text-muted-foreground">探测地址取自</span>
                      {HEALTH_PROBE_TARGETS.map((target) => (
                        <Button
                          key={target}
                          type="button"
                          variant={form.health_probe_target === target ? 'secondary' : 'ghost'}
                          size="sm"
                          className="h-7 px-2 text-xs"
                          onClick={() => setField('health_probe_target', target)}
                          aria-pressed={form.health_probe_target === target}
                        >
                          {TARGET_LABELS[target]}
                        </Button>
                      ))}
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <Input
                        value={form.health_probe_status}
                        onChange={(e) => setField('health_probe_status', e.target.value)}
                        placeholder="期望状态码（留空=不校验）"
                        aria-label="期望状态码"
                        inputMode="numeric"
                      />
                      <Input
                        value={form.health_probe_latency}
                        onChange={(e) => setField('health_probe_latency', e.target.value)}
                        placeholder="最大耗时 ms（留空=不限）"
                        aria-label="最大耗时毫秒"
                        inputMode="numeric"
                      />
                    </div>
                    <Input
                      value={form.health_probe_body}
                      onChange={(e) => setField('health_probe_body', e.target.value)}
                      placeholder={'响应体须包含（固定字符串，如 "status":"ok"）'}
                      aria-label="响应体须包含"
                    />
                    <p className="text-xs text-muted-foreground">
                      只支持状态码 / 耗时 / 固定字符串三条。
                      <span className="font-medium text-foreground">
                        不要在这里放 token 或请求头
                      </span>
                      ——需要凭据的检查该交给服务自身或独立探测工具。
                    </p>
                  </div>
                )}
              </div>
            </Field>

            <Separator />

            <Field label="家庭内网 URL">
              <Input
                value={form.home}
                onChange={(e) => setField('home', e.target.value)}
                placeholder="http://192.168.50.100:端口"
              />
            </Field>
            <Field label="Tailscale URL">
              <Input value={form.tailscale} onChange={(e) => setField('tailscale', e.target.value)} />
            </Field>
            <Field label="公网域名 URL">
              <Input value={form.domain} onChange={(e) => setField('domain', e.target.value)} />
            </Field>

            {probe && (
              <div className="rounded-md border bg-muted/40 p-3 text-xs font-mono">
                {(['home', 'tailscale', 'domain'] as const).map((k) => {
                  const item = probe[k];
                  const labels = { home: '家庭', tailscale: 'TC', domain: '公网' };
                  if (!item?.configured) return <div key={k}>{labels[k]}：—</div>;
                  return (
                    <div key={k}>
                      {labels[k]}：{item.ok ? '✅' : '❌'}
                      {item.msg && !item.ok ? ` (${item.msg})` : ''}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <SheetFooter className="mt-6 flex-row items-center gap-2">
            {mode === 'edit' && service && (
              <Button
                type="button"
                variant="destructive"
                onClick={() => setConfirmDelete(true)}
                disabled={saving}
              >
                删除
              </Button>
            )}
            <div className="ml-auto flex gap-2">
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
                取消
              </Button>
              <Button
                type="button"
                onClick={handleSave}
                disabled={saving || !form.name.trim() || !form.machine.trim()}
              >
                {saving ? '保存中…' : '保存并探测'}
              </Button>
            </div>
          </SheetFooter>
        </SheetContent>
      </Sheet>

      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除服务？</AlertDialogTitle>
            <AlertDialogDescription>
              将删除服务 <span className="font-mono">{service?.name}</span> 的配置。操作不可撤销。
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

function Field({ label, required, hint, children }: { label: string; required?: boolean; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label>
        {label}
        {required && <span className="ml-1 text-destructive">*</span>}
      </Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}
