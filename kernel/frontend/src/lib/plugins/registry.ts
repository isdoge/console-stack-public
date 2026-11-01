import { CalendarClock, ClipboardList, Puzzle, Rss, Wallet, type LucideIcon } from 'lucide-react';
import { lazy, type ComponentType, type ReactNode, type MutableRefObject } from 'react';
import composition from '@plugins/composition.json';
import { assertNoPluginPathConflicts, normalizePluginManifest, type PluginManifest } from './manifest';
import type { PluginNavigation } from './navigation-types';
export type { PluginManifest } from './manifest';

const PLUGIN_ID_PATTERN = /^[a-z][a-z0-9_]{0,47}$/;

export interface PluginState extends PluginManifest { enabled: boolean; visible: boolean; available?: boolean; error?: string | null }
export interface PluginRouteProps { refreshKey: number; refreshForce?: boolean; active?: boolean }
export interface PluginFrontend {
  default: ComponentType<PluginRouteProps>;
  Settings?: ComponentType<PluginSettingsProps>;
  Sidebar?: ComponentType<{ collapsed?: boolean; onNavigate?: () => void }>;
  loadNavigation?: () => Promise<PluginNavigation>;
}

export async function loadRequiredPluginExport<K extends keyof PluginFrontend>(
  id: string,
  key: K,
  load: (() => Promise<PluginFrontend>) | undefined,
  declared: boolean,
): Promise<NonNullable<PluginFrontend[K]>> {
  const frontend = await load?.();
  const component = frontend?.[key];
  if (!component && declared) throw new Error(`插件 ${id} 声明了 ${key}，但入口未导出`);
  if (!component) throw new Error(`插件 ${id} 缺少前端入口`);
  return component as NonNullable<PluginFrontend[K]>;
}

export function parseDeclaredComposition(document: unknown): string[] {
  if (
    !document ||
    typeof document !== 'object' ||
    Array.isArray(document) ||
    Object.keys(document).length !== 1 ||
    !Array.isArray((document as { plugins?: unknown }).plugins)
  ) {
    throw new Error('插件组合清单顶层必须只含 plugins 数组');
  }
  const declared = (document as { plugins: unknown[] }).plugins;
  const seen = new Set<string>();
  for (const id of declared) {
    if (typeof id !== 'string' || !PLUGIN_ID_PATTERN.test(id)) {
      throw new Error(`插件组合清单含无效插件 id：${String(id)}`);
    }
    if (seen.has(id)) {
      throw new Error(`插件组合清单插件 id 重复：${id}`);
    }
    seen.add(id);
  }
  return declared as string[];
}

export function resolveRegisteredPlugins(
  manifestMap: Record<string, unknown>,
  entryMap: Record<string, () => Promise<PluginFrontend>>,
  declaredComposition: unknown,
) {
  const declaredIds = parseDeclaredComposition(declaredComposition);
  const declaredSet = new Set(declaredIds);
  const discoveredById = new Map<string, { path: string; manifest: PluginManifest }>();

  for (const [manifestPath, raw] of Object.entries(manifestMap)) {
    const directoryId = manifestPath.match(/\/([^/]+)\/manifest\.json$/)?.[1];
    if (!directoryId) throw new Error(`插件清单路径无效：${manifestPath}`);
    if (!declaredSet.has(directoryId)) continue;
    const manifest = normalizePluginManifest(raw, directoryId);
    discoveredById.set(directoryId, { path: manifestPath, manifest });
  }

  const missing = declaredIds.filter((id) => !discoveredById.has(id));
  if (missing.length > 0) {
    throw new Error(`组合清单声明了不存在的插件：${missing.join(', ')}`);
  }

  const normalized = declaredIds.map((id) => discoveredById.get(id)!);
  assertNoPluginPathConflicts(normalized.map(({ manifest }) => manifest));

  return normalized.map(({ path: manifestPath, manifest }) => {
    const load = entryMap[manifestPath.replace(/\/manifest\.json$/, '/frontend/index.tsx')];
    if (!load) throw new Error(`插件 ${manifest.id} 缺少前端入口`);
    return {
      ...manifest,
      loadNavigation: async () => {
        const loadItems = await loadRequiredPluginExport(manifest.id, 'loadNavigation', load, manifest.navigation);
        return loadItems();
      },
      Component: lazy(async () => ({ default: await loadRequiredPluginExport(manifest.id, 'default', load, true) as ComponentType<PluginRouteProps> })),
      Settings: lazy<ComponentType<PluginSettingsProps>>(async () => ({ default: await loadRequiredPluginExport(manifest.id, 'Settings', load, Boolean(manifest.settings)) as ComponentType<PluginSettingsProps> })),
      Sidebar: lazy<ComponentType<{ collapsed?: boolean; onNavigate?: () => void }>>(async () => ({ default: await loadRequiredPluginExport(manifest.id, 'Sidebar', load, manifest.sidebar) as ComponentType<{ collapsed?: boolean; onNavigate?: () => void }> })),
    };
  }).sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
}

const manifests = import.meta.glob<PluginManifest>('@plugins/*/manifest.json', { eager: true, import: 'default' });
const entries = import.meta.glob<PluginFrontend>('@plugins/*/frontend/index.tsx');
export const plugins = resolveRegisteredPlugins(manifests, entries, composition);

export function pluginIsAvailable(state: Pick<PluginState, 'available'> | undefined): boolean {
  return state?.available !== false;
}

export function pluginForPath(path: string) {
  return plugins.find((plugin) => plugin.routes.some((route) => {
    const prefix = route.split('/:')[0];
    return path === prefix || path.startsWith(prefix + '/');
  }));
}

export interface ToolbarRegistration { owner: string; node: ReactNode }

export interface PluginSettingsStatus { dirty: boolean; problems: number; errors?: string[]; retry?: () => Promise<void> }
export interface PluginSettingsProps {
  enabled: boolean;
  refreshKey: number;
  busy: boolean;
  mutation: { begin: () => number | null; finish: (token: number) => boolean; active: MutableRefObject<number | null>; epoch: MutableRefObject<number> };
  onBusyChange: (busy: boolean) => void;
  onStatus: (status: PluginSettingsStatus) => void;
}

const ICONS: Record<string, LucideIcon> = { rss: Rss, wallet: Wallet, 'calendar-clock': CalendarClock, 'clipboard-list': ClipboardList };
export function pluginIcon(name?: string) { return ICONS[name || ''] || Puzzle; }
