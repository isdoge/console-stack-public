import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { BrowserRouter, Route, Routes, useLocation } from 'react-router-dom';
import { Background } from '@/components/background';
import { ErrorBoundary } from '@/components/error-boundary';
import { Skeleton } from '@/components/ui/skeleton';
import { Sidebar } from '@/components/layout/sidebar';
import { Topbar } from '@/components/layout/topbar';
import { CommandPalette } from '@/components/command-palette';
import { Toaster } from '@/components/ui/sonner';
import { useDashboardEditing } from '@/components/dashboard/edit-mode';
// 首页保持同步打包；其余路由懒加载分片，避免主 chunk 过大
import { plugins, pluginForPath } from '@/lib/plugins/registry';
import { PluginProvider, PluginChromeProvider, usePlugins, usePluginChrome } from '@/lib/plugins/provider';
import { pluginIsAvailable } from '@/lib/plugins/registry';
import { PluginErrorBoundary } from '@/lib/plugins/error-boundary';
import { Link } from 'react-router-dom';
import { DashboardRoute } from '@/routes/dashboard';

const RESUME_PAIR_WINDOW_MS = 1_000;


const DevicesRoute = lazy(() =>
  import('@/routes/devices').then((m) => ({ default: m.DevicesRoute })),
);



const SettingsRoute = lazy(() =>
  import('@/routes/settings').then((m) => ({ default: m.SettingsRoute })),
);
const NotFoundRoute = lazy(() =>
  import('@/routes/not-found').then((m) => ({ default: m.NotFoundRoute })),
);

function RouteFallback() {
  return (
    <div className="space-y-3 p-2">
      <Skeleton className="h-8 w-40" />
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

function Shell({ onOpenPalette }: { onOpenPalette: () => void }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const [refreshForce, setRefreshForce] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const loc = useLocation();
  const { states, loading: pluginsLoading, error: pluginsError, reload } = usePlugins();
  const { toolbar } = usePluginChrome();
  const activePlugin = pluginForPath(loc.pathname);
  const pluginState = (id: string) => states.find((plugin) => plugin.id === id);
  const enabled = (id: string) => Boolean(pluginState(id)?.enabled && pluginIsAvailable(pluginState(id)));
  const isImmersive = Boolean(activePlugin?.immersive && enabled(activePlugin.id));
  const isDevices = loc.pathname === '/devices' || loc.pathname.startsWith('/devices/');

  const triggerRefresh = useCallback((kind: 'manual' | 'resume') => {
    setRefreshing(true);
    setRefreshForce(kind === 'manual');
    setRefreshKey((n) => n + 1);
    clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = setTimeout(() => {
      refreshTimerRef.current = undefined;
      setRefreshing(false);
    }, 400);
  }, []);

  // A6：布局编辑态期间跳过恢复刷新——refreshKey 一变卡片整片重渲染会打断拖拽手势
  // （与 Topbar 手动刷新按钮的禁用条件同一读数）。用 ref 而非把 editing 放进下面
  // effect 的依赖数组，避免编辑态每次切换都重新增删 DOM 监听。
  const editing = useDashboardEditing();
  const editingRef = useRef(editing);
  useEffect(() => {
    editingRef.current = editing;
  }, [editing]);

  // PWA/iOS 主屏从后台恢复前台时，用既有 refreshKey 主动刷新一次，减少看到旧数据的时间。
  // 只认两种恢复信号：可见性变为 visible、以及从 bfcache 恢复的 persisted pageshow；
  // 普通首次 pageshow（非 persisted）不触发，避免首屏重复刷新。不引入新请求层/缓存。
  useEffect(() => {
    type ResumeSignal = 'visible' | 'pageshow';
    let pending: { signal: ResumeSignal; at: number } | undefined;
    const resetResume = () => { pending = undefined; };
    const refreshOnResume = (signal: ResumeSignal) => {
      if (editingRef.current) {
        resetResume();
        return;
      }
      const now = performance.now();
      // 只配对 1 秒内的异类信号；hidden/pagehide 切开两次恢复，同类信号始终独立。
      // 手动刷新不经过这里，因此不会被恢复信号配对吞掉。
      if (pending && pending.signal !== signal && now - pending.at < RESUME_PAIR_WINDOW_MS) {
        resetResume();
        return;
      }
      pending = { signal, at: now };
      triggerRefresh('resume');
    };
    const handleVisible = () => {
      if (document.visibilityState !== 'visible') {
        resetResume();
        return;
      }
      refreshOnResume('visible');
    };
    const handlePageShow = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      refreshOnResume('pageshow');
    };
    document.addEventListener('visibilitychange', handleVisible);
    window.addEventListener('pageshow', handlePageShow);
    window.addEventListener('pagehide', resetResume);
    return () => {
      document.removeEventListener('visibilitychange', handleVisible);
      window.removeEventListener('pageshow', handlePageShow);
      window.removeEventListener('pagehide', resetResume);
      clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = undefined;
    };
  }, [triggerRefresh]);

  return (
    <div className="relative flex min-h-screen text-foreground">
      <Sidebar />
      {/* min-w-0：flex item 默认 min-width:auto，手机上宽表格/卡片会把主列撑出视口（T10） */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* 沉浸式插件自行提供顶栏。 */}
        {!isImmersive && <Topbar onRefresh={() => triggerRefresh('manual')} refreshing={refreshing} onOpenPalette={onOpenPalette} panelSessions={toolbar?.node} />}
        <main
          className={
            isImmersive ? 'flex flex-1 flex-col overflow-hidden' : isDevices
              ? 'flex-1 overflow-x-hidden px-3 py-6 sm:px-6'
              : 'flex-1 overflow-x-hidden px-6 py-6'
          }
        >
          {plugins.filter((p) => p.persistent && enabled(p.id)).map((plugin) => (
            <PluginErrorBoundary key={plugin.id} pluginId={plugin.id} pluginName={plugin.name} kind="页面"><Suspense fallback={activePlugin?.id === plugin.id ? <RouteFallback /> : null}>
              <plugin.Component refreshKey={refreshKey} refreshForce={refreshForce} active={activePlugin?.id === plugin.id} />
            </Suspense></PluginErrorBoundary>
          ))}
          {activePlugin && (!enabled(activePlugin.id) || !pluginIsAvailable(pluginState(activePlugin.id))) ? (
            <div className="mx-auto max-w-lg space-y-4 py-12">
              <h2 className="text-lg font-semibold">{pluginsLoading ? '正在读取插件状态…' : pluginsError ? '插件状态暂不可用' : pluginState(activePlugin.id)?.enabled ? `${activePlugin.name}暂不可用` : `${activePlugin.name}已停用`}</h2>
              <p className="text-sm text-muted-foreground">{pluginsError || pluginState(activePlugin.id)?.error || (pluginState(activePlugin.id)?.enabled ? '插件运行故障，宿主已暂停。' : '已有数据保留，可以在设置中重新启用。')}</p>
              <div className="flex gap-4"><Link className="underline" to="/settings#plugins">管理插件</Link>{pluginsError && <button className="underline" onClick={() => void reload()}>重试</button>}</div>
            </div>
          ) : !isImmersive && (
            // key 挂路径：切页时 ErrorBoundary 状态重置，崩溃兜底不跨页残留
            <ErrorBoundary key={loc.pathname}>
              <Suspense fallback={<RouteFallback />}>
                <Routes>
                  <Route path="/" element={<DashboardRoute refreshKey={refreshKey} refreshForce={refreshForce} />} />
                  <Route path="/devices" element={<DevicesRoute refreshKey={refreshKey} refreshForce={refreshForce} />} />
                  <Route path="/devices/:id" element={<DevicesRoute refreshKey={refreshKey} refreshForce={refreshForce} />} />
                  {plugins.filter((p) => !p.persistent && enabled(p.id)).flatMap((plugin) => plugin.routes.map((path) => <Route key={path} path={path} element={<PluginErrorBoundary pluginId={plugin.id} pluginName={plugin.name} kind="页面"><plugin.Component refreshKey={refreshKey} refreshForce={refreshForce} /></PluginErrorBoundary>} />))}
                  <Route path="/settings" element={<SettingsRoute refreshKey={refreshKey} />} />
                  <Route path="*" element={<NotFoundRoute />} />
                </Routes>
              </Suspense>
            </ErrorBoundary>
          )}
        </main>
      </div>
    </div>
  );
}

export default function App() {
  // T12 分支：命令面板开关信号（顶栏按钮 + Ctrl/Cmd+K 都会触发）
  const [paletteSignal, setPaletteSignal] = useState(0);
  return (
    <BrowserRouter><PluginProvider><PluginChromeProvider>
      <Background />
      <Shell onOpenPalette={() => setPaletteSignal((s) => s + 1)} />
      {/* 面板页也保留命令面板（Ctrl/Cmd+K 随时可用） */}
      <CommandPalette openSignal={paletteSignal} />
      <Toaster />
    </PluginChromeProvider></PluginProvider></BrowserRouter>
  );
}
