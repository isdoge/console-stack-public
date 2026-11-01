import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { plugins, pluginIsAvailable, type PluginState, type ToolbarRegistration } from './registry';
import { PluginErrorBoundary } from './error-boundary';

interface PluginContextValue {
  states: PluginState[];
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  update: (id: string, patch: Pick<Partial<PluginState>, 'enabled' | 'visible'>) => Promise<void>;
}
// Standalone components/tests retain the installed defaults. The real App always provides authoritative state.
const fallback: PluginContextValue = {
  states: plugins.map((p) => ({ ...p, enabled: true, visible: true })), loading: false, error: null,
  reload: async () => {}, update: async () => {},
};
const PluginContext = createContext<PluginContextValue>(fallback);

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal, cache: 'no-store', headers: { 'Content-Type': 'application/json', ...init?.headers } });
    const data = await response.json();
    if (!response.ok) throw new Error(data?.error || '插件设置请求失败');
    return data;
  } finally {
    clearTimeout(timer);
  }
}

export function PluginProvider({ children }: { children: ReactNode }) {
  const [states, setStates] = useState<PluginState[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const pending = useRef(false);
  const mounted = useRef(true);
  const reload = useCallback(async () => {
    if (pending.current) return;
    const current = ++generation.current;
    try {
      const response = await request<PluginState[]>('/api/plugins');
      if (!Array.isArray(response) || response.some((row) => typeof row.enabled !== 'boolean' || typeof row.visible !== 'boolean' || (row.available !== undefined && typeof row.available !== 'boolean') || (row.error !== undefined && row.error !== null && typeof row.error !== 'string'))) throw new Error('插件清单响应无效');
      const rows = response.map((row) => ({ ...row, available: pluginIsAvailable(row), error: row.error ?? null }));
      if (!mounted.current || current !== generation.current) return;
      setStates(rows); setError(null);
    } catch (err) {
      if (!mounted.current || current !== generation.current) return;
      setError(err instanceof Error ? err.message : '插件设置暂不可用');
      // A failed refresh cannot preserve potentially disabled plugins as active.
      setStates([]);
    } finally {
      if (mounted.current && current === generation.current) setLoading(false);
    }
  }, []);
  const update = useCallback(async (id: string, patch: Pick<Partial<PluginState>, 'enabled' | 'visible'>) => {
    if (pending.current) throw new Error('正在保存，请稍后');
    pending.current = true; ++generation.current;
    try {
      await request<PluginState>(`/api/plugins/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(patch) });
    } finally {
      pending.current = false;
      await reload(); // write outcome unknown: read back authority before presenting state
    }
  }, [reload]);
  useEffect(() => {
    mounted.current = true;
    void reload();
    const refresh = () => { if (document.visibilityState === 'visible') void reload(); };
    const interval = window.setInterval(refresh, 15000);
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => { mounted.current = false; clearInterval(interval); window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); };
  }, [reload]);
  const value = useMemo(() => ({ states, loading, error, reload, update }), [states, loading, error, reload, update]);
  return <PluginContext.Provider value={value}>{children}</PluginContext.Provider>;
}
export function usePlugins() { return useContext(PluginContext); }
export function usePluginEnabled(id: string) { return usePlugins().states.some((p) => p.id === id && p.enabled && pluginIsAvailable(p)); }
export function usePluginVisible(id: string) { return usePlugins().states.some((p) => p.id === id && p.enabled && p.visible && pluginIsAvailable(p)); }

interface PluginChromeValue { toolbar: ToolbarRegistration | null; registerToolbar: (owner: string, node: ReactNode | null) => void }
const ToolbarContext = createContext<PluginChromeValue>({ toolbar: null, registerToolbar: () => {} });
export function PluginChromeProvider({ children }: { children: ReactNode }) {
  const { states } = usePlugins();
  const [registrations, setRegistrations] = useState<Record<string, ReactNode>>({});
  const registerToolbar = useCallback((owner: string, node: ReactNode | null) => {
    setRegistrations((current) => {
      if (node === null) {
        if (!(owner in current)) return current;
        const next = { ...current };
        delete next[owner];
        return next;
      }
      if (current[owner] === node) return current;
      return { ...current, [owner]: node };
    });
  }, []);
  const toolbarRows = useMemo(() => Object.entries(registrations).sort(([a], [b]) => {
    const ai = plugins.findIndex((plugin) => plugin.id === a);
    const bi = plugins.findIndex((plugin) => plugin.id === b);
    return (ai < 0 ? Number.MAX_SAFE_INTEGER : ai) - (bi < 0 ? Number.MAX_SAFE_INTEGER : bi) || a.localeCompare(b);
  }).filter(([owner]) => {
    const state = states.find((plugin) => plugin.id === owner);
    return plugins.some((plugin) => plugin.id === owner) && Boolean(state?.enabled && state.visible && pluginIsAvailable(state));
  }), [registrations, states]);
  useLayoutEffect(() => {
    const activeOwners = new Set(states.filter((state) => state.enabled && state.visible && pluginIsAvailable(state)).map((state) => state.id));
    setRegistrations((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([owner]) => activeOwners.has(owner) && plugins.some((plugin) => plugin.id === owner)));
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
  }, [states]);
  const toolbar = useMemo(() => toolbarRows.length ? { owner: 'plugins', node: <>{toolbarRows.map(([owner, node]) => <PluginErrorBoundary key={owner} pluginId={owner} pluginName={plugins.find((plugin) => plugin.id === owner)?.name ?? owner} kind="工具栏">{node}</PluginErrorBoundary>)}</> } : null, [toolbarRows]);
  const value = useMemo(() => ({ toolbar, registerToolbar }), [toolbar, registerToolbar]);
  return <ToolbarContext.Provider value={value}>{children}</ToolbarContext.Provider>;
}
export function usePluginChrome() { return useContext(ToolbarContext); }
