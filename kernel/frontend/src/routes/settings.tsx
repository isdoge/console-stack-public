import { PluginManager } from '@/lib/plugins/plugin-manager';
import { usePlugins } from '@/lib/plugins/provider';
import { pluginIsAvailable, plugins, type PluginSettingsStatus } from '@/lib/plugins/registry';
import { PluginErrorBoundary } from '@/lib/plugins/error-boundary';
import { DisplayTime } from '@/components/ui/display-time';
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

import { Input } from '@/components/ui/input';
import { getConsoleSettings, getEffectiveConfig, saveConsoleSettings, apiErrorMessage } from '@/lib/api';

import type { ConsoleSettingEntry, ConsoleSettings, ConsoleSettingsGroup, ConsoleTableRows, EffectiveConfig, EffectiveConfigField } from '@/lib/types';
import { cn } from '@/lib/utils';


type SettingValues = Record<string, string>;
type SettingsEditor = {
  latest: ConsoleSettings | null;
  baseline: SettingValues;
  drafts: SettingValues;
};
// ---- 页签（Kuma 形态分区：常规 / 通知[预留] / 数据管理 / 面板 / 安全 / 关于）----
// 「外观」= 顶栏的暗色切换，不在设置页重复一份开关。
// 分区常驻挂载、仅按页签切换显隐：草稿、冲突横幅与请求乱序防护都锚在 SettingsRoute 的
// state 与既有分区组件上，卸载非活动分区会把这些语义交给每次切签的重挂载时序。

const SETTINGS_TABS = [
  { id: 'general', label: '常规' },
  { id: 'notify', label: '通知' },
  { id: 'data', label: '数据管理' },
  { id: 'plugins', label: '插件' },
  ...plugins.filter((p) => p.settings).map((p) => ({ id: p.settings!.id, label: p.settings!.label })),
  { id: 'security', label: '安全' },
  { id: 'about', label: '关于' },
] as const;
type SettingsTabId = string;

function isSettingsTabId(value: string): value is SettingsTabId {
  return SETTINGS_TABS.some((tab) => tab.id === value);
}

/** 初始页签取自 location.hash，便于刷新/直达时停在同一分区。 */
function initialTab(): SettingsTabId {
  const hash = window.location.hash.replace(/^#/, '');
  return isSettingsTabId(hash) ? hash : 'general';
}

function TabPanel({
  id,
  active,
  children,
}: {
  id: SettingsTabId;
  active: boolean;
  children: ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={`settings-tabpanel-${id}`}
      aria-labelledby={`settings-tab-${id}`}
      className={cn('space-y-4', !active && 'hidden')}
    >
      {children}
    </div>
  );
}

function SectionFallback({ name }: { name: string }) {
  return (
    <p className="py-8 text-center text-sm text-muted-foreground">{name}尚未加载，请重试加载。</p>
  );
}

// ---- 值问题的自动标记（越界 / 过期 / 悬空引用）----

/** 与后端 settings_store 的整数校验同口径：空串 = 恢复默认，合法。 */
function settingProblem(entry: ConsoleSettingEntry, draft: string): string | null {
  if (!entry.writable || entry.type !== 'int') return null;
  const text = draft.trim();
  if (text === '') return null;
  if (!/^-?\d+$/.test(text)) return '必须是整数';
  const value = Number(text);
  if ((entry.min != null && value < entry.min) || (entry.max != null && value > entry.max)) {
    return `必须在 ${entry.min}–${entry.max} 之间（${entry.unit}）`;
  }
  return null;
}

/** 面板行的问题清单；registryIds 为 null 表示台账未加载，此时不判「设备不在台账」以免误报。 */
const PROBE_STALE_FALLBACK_SECONDS = 1800;

/** 探针超过两个采样周期没跑视为过期（probe-monitor 同口径）；返回 null = 正常。 */
function probeStaleText(lastRun: string | null, intervalSeconds: number | null): string | null {
  if (!lastRun) return '探针从未运行';
  const ts = Date.parse(lastRun);
  if (Number.isNaN(ts)) return '时间无法解析';
  const ageSeconds = (Date.now() - ts) / 1000;
  if (ageSeconds <= 2 * (intervalSeconds ?? PROBE_STALE_FALLBACK_SECONDS)) return null;
  const age =
    ageSeconds >= 48 * 3600
      ? `${Math.floor(ageSeconds / 86400)} 天`
      : ageSeconds >= 3600
        ? `${Math.floor(ageSeconds / 3600)} 小时`
        : `${Math.floor(ageSeconds / 60)} 分钟`;
  return `已 ${age}未更新`;
}

// ---- 关于 / 有效配置（C10-a，只读分区） ----

/** 来源三态徽章：env 强调色 / yaml 中性 / default 弱化。 */
function SourceBadge({ field }: { field: EffectiveConfigField }) {
  const variant =
    field.source === 'env' ? 'success' : field.source === 'yaml' ? 'secondary' : 'outline';
  const label =
    field.source === 'env' ? 'env' : field.source === 'yaml' ? 'yaml' : '默认';
  return <Badge variant={variant}>{label}</Badge>;
}

function ConfigFieldRow({ field }: { field: EffectiveConfigField }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b py-1.5 text-sm last:border-0">
      <div className="min-w-0">
        <div className="font-mono text-xs text-muted-foreground">{field.key}</div>
        {field.machine && <div className="text-xs text-muted-foreground">{field.machine}</div>}
      </div>
      <div className="flex items-center gap-2">
        <span className="max-w-[26rem] truncate font-mono text-xs">
          {field.value === '' ? '（未设置）' : String(field.value)}
        </span>
        <SourceBadge field={field} />
      </div>
    </div>
  );
}

function EffectiveConfigSection({ data, staleText }: { data: EffectiveConfig; staleText: string | null }) {
  const groups = Object.entries(data.groups ?? {});
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">关于 / 有效配置</CardTitle>
        <CardDescription>
          只读视图：每个字段的值与来源（env &gt; yaml &gt; 默认）。敏感值已遮蔽（***）。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 md:grid-cols-2">
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
            <span>
              DB schema 版本：
              <span className={cn('font-mono', data.schema_version === null && 'text-amber-600 dark:text-amber-400')}>
                {data.schema_version ?? '未知'}
              </span>
            </span>
            <span>
              running commit：<span className="font-mono">{data.running_commit ?? '未知'}</span>
            </span>
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
            <span className="inline-flex flex-wrap items-center gap-1.5">
              探针上次运行：
              <span className={cn('font-mono', staleText && 'text-amber-600 dark:text-amber-400')}>
                {data.last_probe_run ? <DisplayTime value={data.last_probe_run} /> : '未运行'}
              </span>
              {staleText && <Badge variant="warning">过期：{staleText}</Badge>}
            </span>
            <span>
              生成时间：<span className="font-mono"><DisplayTime value={data.generated_at} /></span>
            </span>
          </div>
        </div>

        {data.machines.length > 0 && (
          <div>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">机器台账（machines.yaml）</h3>
            {data.machines.map((field) => (
              <ConfigFieldRow key={field.key} field={field} />
            ))}
          </div>
        )}

        {groups.map(([groupId, group]) => (
          <div key={groupId}>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">{group.title}</h3>
            {group.entries.map((field) => (
              <ConfigFieldRow key={field.key} field={field} />
            ))}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// ---- 可写设置（C10-b：白名单 → settings.yaml） ----

/**
 * 一个可写项。`writable=false` 说明 `.env` / compose 已经设了它——按 C10-a 定下的
 * 「env 已设的项 UI 只读」规则，这里只显示值和原因，不给输入框（后端也会 409 拒绝）。
 */
function SettingEntryRow({
  entry,
  draft,
  onChange,
}: {
  entry: ConsoleSettingEntry;
  draft: string;
  onChange: (value: string) => void;
}) {
  const problem = settingProblem(entry, draft);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b py-2 last:border-0">
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">
          {entry.title}
          {entry.requires_restart && (
            <span className="ml-2 text-xs font-normal text-muted-foreground">改后需重启容器</span>
          )}
        </div>
        <div className="text-xs text-muted-foreground">{entry.description}</div>
        <div className="font-mono text-[11px] text-muted-foreground">
          {entry.key}
          {entry.type === 'int' && entry.min != null && entry.max != null
            ? `（${entry.min}–${entry.max} ${entry.unit}，默认 ${entry.default}）`
            : ''}
        </div>
        {problem && (
          <div className="mt-0.5 flex items-center gap-1 text-xs text-destructive" role="status">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            {problem}
          </div>
        )}
      </div>
      <div className="flex items-center gap-2">
        {entry.writable ? (
          <>
            <Input
              className={cn('h-8 w-32', problem && 'border-destructive')}
              type={entry.type === 'int' ? 'number' : 'text'}
              value={draft}
              min={entry.min ?? undefined}
              max={entry.max ?? undefined}
              placeholder={entry.default}
              aria-label={entry.title}
              aria-invalid={problem ? true : undefined}
              onChange={(e) => onChange(e.target.value)}
            />
            <Badge variant={entry.source === 'settings' ? 'secondary' : 'outline'}>
              {entry.source === 'settings' ? 'settings.yaml' : '默认'}
            </Badge>
          </>
        ) : (
          <>
            <span className="font-mono text-xs" title={entry.readonly_reason ?? undefined}>
              {entry.value}
            </span>
            <Badge variant="success">env</Badge>
          </>
        )}
      </div>
    </div>
  );
}

/** 可写分区（常规 / 数据管理共用）：只提交改过的键，空串 = 恢复默认。 */
function WritableSection({
  title,
  description,
  group,
  drafts,
  baseline,
  setDraft,
  onSave,
  onLoadLatest,
  saving,
  busy,
  children,
}: {
  title: string;
  description: string;
  group?: ConsoleSettingsGroup;
  drafts: SettingValues;
  baseline: SettingValues;
  setDraft: (key: string, value: string) => void;
  onSave: (keys: string[]) => void;
  onLoadLatest: (keys: string[]) => void;
  saving: boolean;
  busy: boolean;
  children?: ReactNode;
}) {
  const entries = group?.entries ?? [];
  const writableKeys = entries.filter((e) => e.writable).map((e) => e.key);
  const dirtyEntries = entries.filter((entry) => drafts[entry.key] !== baseline[entry.key]);
  const dirty = dirtyEntries.filter((entry) => entry.writable).map((entry) => entry.key);
  const conflict = dirtyEntries.some((entry) => entry.value !== baseline[entry.key]);
  return (
    <Card role="region" aria-label={title}>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="text-base">{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </div>
          {writableKeys.length > 0 && (
            <Button
              type="button"
              size="sm"
              disabled={busy || dirty.length === 0}
              onClick={() => onSave(dirty)}
            >
              {saving ? '保存中…' : dirty.length > 0 ? `保存（${dirty.length}）` : '保存'}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {dirtyEntries.length > 0 && (
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs" role="status">
            <span className={conflict ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'}>
              {conflict ? '服务器设置已变化，已保留本分区草稿。' : '本分区有未保存的修改，刷新会保留草稿。'}
            </span>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              title="用最近一次成功加载的服务器数据替换本分区草稿"
              onClick={() => onLoadLatest(entries.map((entry) => entry.key))}
            >
              载入最新
            </Button>
          </div>
        )}
        {entries.map((entry) => (
          <SettingEntryRow
            key={entry.key}
            entry={entry}
            draft={drafts[entry.key] ?? ''}
            onChange={(value) => setDraft(entry.key, value)}
          />
        ))}
        {children}
      </CardContent>
    </Card>
  );
}

/** 数据管理的只读部分：各表行数。不做 VACUUM（评审已裁决）。 */
function TableRowsList({ tables }: { tables: ConsoleTableRows[] }) {
  if (tables.length === 0) return null;
  return (
    <div className="pt-3">
      <h3 className="mb-1 text-xs font-semibold text-muted-foreground">各表行数</h3>
      <div className="grid gap-x-6 gap-y-0.5 sm:grid-cols-2 lg:grid-cols-3">
        {tables.map((t) => (
          <div key={t.name} className="flex justify-between gap-2 text-xs">
            <span className="truncate font-mono text-muted-foreground">{t.name}</span>
            <span className="font-mono tabular-nums">{t.rows}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * 服务端视图 → 编辑草稿。**模块级纯函数**：`load` 里只要调用了组件内定义的函数，
 * `react-hooks/exhaustive-deps` 就不再认为 `load` 稳定，会要求把它塞进 useEffect 依赖。
 */
function draftsFromSettings(data: ConsoleSettings): SettingValues {
  const next: SettingValues = {};
  for (const group of Object.values(data.groups ?? {})) {
    for (const entry of group.entries) next[entry.key] = entry.value;
  }
  return next;
}

function receiveSettings(
  editor: SettingsEditor,
  latest: ConsoleSettings,
  submitted?: SettingValues
): SettingsEditor {
  const values = draftsFromSettings(latest);
  const baseline = { ...editor.baseline };
  const drafts = { ...editor.drafts };
  for (const [key, value] of Object.entries(values)) {
    if (submitted) {
      if (!(key in submitted)) continue; // 保存一个分区不能推进其它字段的编辑基线。
      baseline[key] = value;
      if (drafts[key] === submitted[key]) drafts[key] = value;
    } else if (!(key in baseline) || drafts[key] === baseline[key]) {
      baseline[key] = value;
      drafts[key] = value;
    }
  }
  return { latest, baseline, drafts };
}

/** 「安全」分区：只列**永不可写**的键名，永不回显值。 */
function SecuritySection({ keys }: { keys: string[] }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">安全</CardTitle>
        <CardDescription>
          以下配置**永不**经 UI 写入，只能改 `.env` 后重启容器；一切 token / password /
          secret / key 永远 env-only。这里只列键名，不回显任何值。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap gap-1.5">
          {keys.map((key) => (
            <Badge key={key} variant="outline" className="font-mono text-[11px]">
              {key}
            </Badge>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

export function SettingsRoute({ refreshKey }: { refreshKey: number }) {
  const [effective, setEffective] = useState<EffectiveConfig | null>(null);
  const [settingsEditor, setSettingsEditor] = useState<SettingsEditor>({
    latest: null, baseline: {}, drafts: {},
  });
  const { latest: settings, drafts, baseline } = settingsEditor;
  const [savingSettings, setSavingSettings] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadErrors, setLoadErrors] = useState<string[]>([]);
  const mounted = useRef(false);
  const readGeneration = useRef(0);
  const mutationSequence = useRef(0);
  const activeMutation = useRef<number | null>(null);
  const [pluginBusy, setPluginBusy] = useState<Record<string, boolean>>({});
  const busyHandlers = useMemo(() => Object.fromEntries(plugins.map((p) => [p.id, (value: boolean) => setPluginBusy((prev) => ({ ...prev, [p.id]: value }))])), []);
  const busy = Object.values(pluginBusy).some(Boolean) || savingSettings;
  const mutationEpoch = useRef(0);
  const { states: pluginStates } = usePlugins();
  const [extensionStatus, setExtensionStatus] = useState<Record<string, PluginSettingsStatus>>({});
  const statusHandlers = useMemo(() => Object.fromEntries(plugins.map((p) => [p.id, (status: PluginSettingsStatus) => setExtensionStatus((prev) => ({ ...prev, [p.id]: status }))])), []);
  const [tab, setTab] = useState<SettingsTabId>(initialTab);
  // 控制台只在私有网络使用；默认明文显示面板密码，切换只影响本页显示，不改存储。

  const selectTab = (id: SettingsTabId) => {
    setTab(id);
    // 写回 hash 让刷新/直达停在同一分区；replaceState 不产生历史记录、不惊动路由。
    window.history.replaceState(window.history.state, '', `#${id}`);
  };

  const onTablistKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const index = SETTINGS_TABS.findIndex((item) => item.id === tab);
    const offset = event.key === 'ArrowRight' ? 1 : SETTINGS_TABS.length - 1;
    const next = SETTINGS_TABS[(index + offset) % SETTINGS_TABS.length];
    selectTab(next.id);
    document.getElementById(`settings-tab-${next.id}`)?.focus();
  };

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      readGeneration.current += 1;
      activeMutation.current = null;
    };
  }, []);

  const load = useCallback(async () => {
    const generation = ++readGeneration.current;
    setLoading(true);
    const [effectiveConfig, consoleSettings] = await Promise.allSettled([
      getEffectiveConfig(),
      getConsoleSettings(),
    ]);
    if (!mounted.current || generation !== readGeneration.current || activeMutation.current !== null) return;
    const errors: string[] = [];
    if (effectiveConfig.status === 'fulfilled') setEffective(effectiveConfig.value);
    else errors.push('有效配置');
    if (consoleSettings.status === 'fulfilled') {
      setSettingsEditor((prev) => receiveSettings(prev, consoleSettings.value));
    } else errors.push('可写设置');
    setLoadErrors(errors);
    if (errors.length > 0) toast.error(`${errors.join('、')}加载失败`);
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
  }, [refreshKey, load]);

  const setDraft = (key: string, value: string) =>
    setSettingsEditor((prev) => ({ ...prev, drafts: { ...prev.drafts, [key]: value } }));

  const loadLatestSettings = (keys: string[]) => {
    // 采用已经收到的快照，点击时同步决定替换点，不等待另一次 GET。
    setSettingsEditor((prev) => {
      if (!prev.latest) return prev;
      const values = draftsFromSettings(prev.latest);
      const selected = Object.fromEntries(keys.map((key) => [key, values[key]]));
      return {
        ...prev,
        baseline: { ...prev.baseline, ...selected },
        drafts: { ...prev.drafts, ...selected },
      };
    });
  };

  const beginMutation = useCallback(() => {
    if (activeMutation.current !== null) return null;
    mutationEpoch.current += 1;
    const token = ++mutationSequence.current;
    activeMutation.current = token;
    readGeneration.current += 1;
    setLoading(false);
    return token;
  }, []);

  const finishMutation = useCallback((token: number) => {
    if (!mounted.current || activeMutation.current !== token) return false;
    activeMutation.current = null;
    mutationEpoch.current += 1;
    // 保存期间启动的 GET 也可能拿到旧值，完成边界必须再次使它失效。
    readGeneration.current += 1;
    setLoading(false);
    return true;
  }, []);
  const mutation = useMemo(() => ({ begin: beginMutation, finish: finishMutation, active: activeMutation, epoch: mutationEpoch }), [beginMutation, finishMutation]);

  /** 只提交 keys 里的键；后端拒绝时（403/400/409）原样报它的文案。 */
  const saveSettings = async (keys: string[]) => {
    if (keys.length === 0) return;
    const values: SettingValues = {};
    for (const key of keys) values[key] = drafts[key] ?? '';
    const token = beginMutation();
    if (token === null) return;
    setSavingSettings(true);
    let saved: ConsoleSettings;
    try {
      saved = await saveConsoleSettings(values);
    } catch (err: unknown) {
      if (!finishMutation(token)) return;
      setSavingSettings(false);
      toast.error(apiErrorMessage(err) || '保存设置失败');
      return;
    }
    if (!finishMutation(token)) return;
    setSavingSettings(false);
    setSettingsEditor((prev) => receiveSettings(prev, saved, values));
    const pruned = saved.pruned_oplog ?? 0;
    toast.success(pruned > 0 ? `已保存设置，并清理 ${pruned} 条操作日志` : `已保存 ${keys.length} 项设置`);
  };

  const machineProbeSeconds = useMemo(() => {
    for (const group of Object.values(settings?.groups ?? {})) {
      for (const entry of group.entries) {
        if (entry.key === 'CONSOLE_MACHINE_PROBE_INTERVAL') {
          const parsed = Number.parseInt(entry.value, 10);
          return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
        }
      }
    }
    return null;
  }, [settings]);

  // 渲染时刻判定即可：load/刷新都会重渲染，不需要为徽章单开计时器。
  const probeStale = effective ? probeStaleText(effective.last_probe_run, machineProbeSeconds) : null;

  const sectionTabState = (group?: ConsoleSettingsGroup) => {
    const entries = group?.entries ?? [];
    return {
      dirty: entries.filter((entry) => drafts[entry.key] !== baseline[entry.key]).some((entry) => entry.writable),
      problems: entries.reduce(
        (total, entry) => total + (settingProblem(entry, drafts[entry.key] ?? '') ? 1 : 0),
        0
      ),
    };
  };

  const tabState: Record<SettingsTabId, { dirty: boolean; problems: number }> = {
    general: sectionTabState(settings?.groups.general),
    notify: { dirty: false, problems: 0 },
    data: sectionTabState(settings?.groups.data),
    plugins: { dirty: false, problems: 0 },
    ...Object.fromEntries(plugins.filter((p) => p.settings).map((p) => [p.settings!.id, extensionStatus[p.id] ?? { dirty: false, problems: 0 }])),
    security: { dirty: false, problems: 0 },
    about: {
      dirty: false,
      problems: effective
        ? [probeStale, effective.schema_version === null ? '未知' : null].filter(Boolean).length
        : 0,
    },
  };

  const allLoadErrors = [...loadErrors, ...plugins.filter((p) => pluginStates.some((state) => state.id === p.id && state.enabled)).flatMap((p) => extensionStatus[p.id]?.errors ?? [])];
  const retryAll = () => {
    void load();
    for (const status of Object.values(extensionStatus)) void status.retry?.();
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">设置</h1>
        <p className="text-sm text-muted-foreground">
          管理控制台偏好、数据保留和功能插件。修改后按各分区保存，切换页签会保留当前草稿。
        </p>
      </div>

      <div
        role="tablist"
        aria-label="设置分区"
        className="flex w-full flex-wrap items-center gap-1 rounded-md bg-muted p-1 text-muted-foreground"
        onKeyDown={onTablistKeyDown}
      >
        {SETTINGS_TABS.map(({ id, label }) => {
          const active = tab === id;
          const state = tabState[id];
          return (
            <button
              key={id}
              type="button"
              role="tab"
              id={`settings-tab-${id}`}
              aria-selected={active}
              aria-controls={`settings-tabpanel-${id}`}
              tabIndex={active ? 0 : -1}
              className={cn(
                'inline-flex items-center gap-1.5 whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium transition-all',
                active ? 'bg-background text-foreground shadow-sm' : 'hover:text-foreground'
              )}
              onClick={() => selectTab(id)}
            >
              {label}
              {state.dirty && (
                <span className="h-1.5 w-1.5 rounded-full bg-amber-500" title="有未保存的修改" />
              )}
              {state.problems > 0 && (
                <span
                  className="rounded-full bg-destructive px-1.5 text-[10px] font-semibold leading-4 text-destructive-foreground"
                  title="存在需要处理的值"
                >
                  {state.problems}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {loading && settings && (
        <p className="text-xs text-muted-foreground" role="status">正在刷新，未保存的修改会保留…</p>
      )}
      {allLoadErrors.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3 text-sm" role="alert">
          <span>{allLoadErrors.join('、')}加载失败，已保留当前表单和草稿。</span>
          <Button type="button" size="sm" variant="outline" disabled={loading || busy} onClick={retryAll}>
            重试加载
          </Button>
        </div>
      )}

      <TabPanel id="general" active={tab === 'general'}>
        {settings ? (
          <WritableSection
            title="常规"
            description={`可写项落 ${settings.settings_file}（deploy.sh 会收编）；\`.env\` 已设的项 UI 只读。白名单三项均为「每次现读」的键，保存即生效，无需重启容器。`}
            group={settings.groups.general}
            drafts={drafts}
            baseline={baseline}
            setDraft={setDraft}
            onSave={saveSettings}
            onLoadLatest={loadLatestSettings}
            saving={savingSettings}
            busy={busy}
          />
        ) : (
          <SectionFallback name="常规设置" />
        )}
      </TabPanel>

      <TabPanel id="notify" active={tab === 'notify'}>
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">通知</CardTitle>
            <CardDescription>
              预留分区：告警通知由 Beszel 侧配置（Bark / Shoutrrr），凭据不进本页也不进
              settings.yaml。详见 proposals 的通知渠道调研（J-E）。
            </CardDescription>
          </CardHeader>
        </Card>
      </TabPanel>

      <TabPanel id="data" active={tab === 'data'}>
        {settings ? (
          <WritableSection
            title="数据管理"
            description="只做保留期与行数展示：不提供 VACUUM，也不提供任何一键清库。"
            group={settings.groups.data}
            drafts={drafts}
            baseline={baseline}
            setDraft={setDraft}
            onSave={saveSettings}
            onLoadLatest={loadLatestSettings}
            saving={savingSettings}
            busy={busy}
          >
            <TableRowsList tables={settings.tables} />
          </WritableSection>
        ) : (
          <SectionFallback name="数据管理设置" />
        )}
      </TabPanel>

      <TabPanel id="plugins" active={tab === 'plugins'}><PluginManager /></TabPanel>
      {plugins.filter((p) => p.settings).map((plugin) => {
        const state = pluginStates.find((p) => p.id === plugin.id);
        const enabled = Boolean(state?.enabled && pluginIsAvailable(state));
        return <TabPanel key={plugin.id} id={plugin.settings!.id} active={tab === plugin.settings!.id}>
          {!enabled && <p className="text-sm text-muted-foreground">{state?.enabled ? `${plugin.name}插件运行故障：${state.error || '插件暂不可用'}。` : `${plugin.name}插件已停用，请在插件页启用后管理。`}本页未保存的草稿会保留。</p>}
          {state && pluginIsAvailable(state) && <div hidden={!enabled}><PluginErrorBoundary pluginId={plugin.id} pluginName={plugin.name} kind="设置"><Suspense fallback={<p className="text-sm text-muted-foreground">正在加载…</p>}>
            <plugin.Settings enabled={enabled} refreshKey={refreshKey} busy={busy || !enabled} mutation={mutation} onBusyChange={busyHandlers[plugin.id]} onStatus={statusHandlers[plugin.id]} />
          </Suspense></PluginErrorBoundary></div>}
        </TabPanel>;
      })}

      <TabPanel id="security" active={tab === 'security'}>
        {settings ? (
          <SecuritySection keys={settings.never_writable} />
        ) : (
          <SectionFallback name="安全清单" />
        )}
      </TabPanel>

      <TabPanel id="about" active={tab === 'about'}>
        {effective ? (
          <EffectiveConfigSection data={effective} staleText={probeStale} />
        ) : (
          <SectionFallback name="有效配置" />
        )}
      </TabPanel>
    </div>
  );
}
