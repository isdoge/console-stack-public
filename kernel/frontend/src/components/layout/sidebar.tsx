import { Suspense, useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import {
  ChevronLeft,
  ChevronRight,
  HardDrive,
  LayoutGrid,
  Server,
  Settings,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { plugins, pluginIcon } from '@/lib/plugins/registry';
import { usePlugins } from '@/lib/plugins/provider';
import { pluginIsAvailable } from '@/lib/plugins/registry';
import { PluginErrorBoundary } from '@/lib/plugins/error-boundary';

const NAV = [
  { to: '/', label: '总控台', icon: LayoutGrid, end: true },
  { to: '/devices', label: '设备', icon: HardDrive, end: false },
];

const SIDEBAR_KEY = 'console.sidebar.collapsed';
// A3：隐私模式/配额异常时 getItem/setItem 会抛异常。读路径降级为调用点的默认值，
// 写路径静默保留内存态——两种情况下侧栏都继续渲染和响应交互，不把异常向上抛。
function loadSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === '1';
  } catch {
    return false;
  }
}

function safeSetItem(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // 存不进就算了，本次会话内的折叠状态照常在内存里生效
  }
}

const navItemClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    'flex min-h-[44px] items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors',
    isActive
      ? 'bg-accent text-accent-foreground'
      : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
  );

interface SidebarContentProps {
  onNavigate?: () => void;
  /** 迷你图标栏模式（仅桌面侧栏，移动端抽屉始终全宽） */
  collapsed?: boolean;
  /** 传入时底部渲染收起/展开切换按钮 */
  onToggle?: () => void;
}

export function SidebarContent({ onNavigate, collapsed = false, onToggle }: SidebarContentProps) {
  const { states } = usePlugins();
  const visible = plugins.filter((plugin) => states.some((state) => state.id === plugin.id && state.enabled && state.visible && pluginIsAvailable(state)));
  const navigation = [...NAV, ...visible.filter((p) => !p.persistent).map((p) => ({ to: p.routes[0], label: p.name, icon: pluginIcon(p.icon), end: false }))];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className={cn('flex h-14 items-center gap-2 border-b', collapsed ? 'justify-center' : 'px-4')}>
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
          <Server className="h-4 w-4" />
        </div>
        {!collapsed && (
          <div className="font-semibold text-sm leading-tight">
            Console
            <div className="text-xs font-normal text-muted-foreground">自托管运维</div>
          </div>
        )}
      </div>

      <nav className="flex-1 space-y-1 overflow-y-auto overflow-x-hidden p-2">
        {navigation.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            title={collapsed ? item.label : undefined}
            className={(state) => cn(navItemClass(state), collapsed && 'justify-center px-2')}
            onClick={onNavigate}
          >
            <item.icon className="h-4 w-4 shrink-0" />
            {!collapsed && item.label}
          </NavLink>
        ))}

        {visible.filter((p) => p.sidebar).map((plugin) => <PluginErrorBoundary key={plugin.id} pluginId={plugin.id} pluginName={plugin.name} kind="侧栏"><Suspense fallback={<p className="px-3 py-2 text-xs text-muted-foreground">正在加载{plugin.name}…</p>}><plugin.Sidebar collapsed={collapsed} onNavigate={onNavigate} /></Suspense></PluginErrorBoundary>)}
      </nav>

      <div className="space-y-1 border-t p-2">
        <NavLink
          to="/settings"
          title={collapsed ? '设置' : undefined}
          className={(state) =>
            cn(navItemClass(state), 'w-full', collapsed ? 'justify-center px-2' : undefined)
          }
          onClick={onNavigate}
        >
          <Settings className="h-4 w-4 shrink-0" />
          {!collapsed && '设置'}
        </NavLink>
        {onToggle && (
          <Button
            variant="ghost"
            size="sm"
            title={collapsed ? '展开侧栏' : '收起侧栏'}
            aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
            className={cn('w-full', collapsed ? 'justify-center px-2' : 'justify-start')}
            onClick={onToggle}
          >
            {collapsed ? (
              <ChevronRight className="h-4 w-4 shrink-0" />
            ) : (
              <ChevronLeft className="mr-2 h-4 w-4 shrink-0" />
            )}
            {!collapsed && '收起侧栏'}
          </Button>
        )}
      </div>
    </div>
  );
}

export function Sidebar() {
  // 迷你图标栏：收起后保留 56px 图标条而非整栏隐藏，状态沿用旧键持久化
  const [collapsed, setCollapsed] = useState(loadSidebarCollapsed);

  useEffect(() => {
    safeSetItem(SIDEBAR_KEY, collapsed ? '1' : '0');
  }, [collapsed]);

  return (
    <aside
      className={cn(
        // 与 sticky 顶栏配套：侧栏也常驻，否则长页面滚动时侧栏跟着走、与顶栏错位；
        // h-screen 让内部 nav 的 overflow-y-auto 真正生效
        'hidden shrink-0 overflow-hidden border-r glass-panel transition-[width] duration-300 md:sticky md:top-0 md:flex md:h-screen md:flex-col',
        collapsed ? 'md:w-14' : 'md:w-56'
      )}
    >
      <SidebarContent collapsed={collapsed} onToggle={() => setCollapsed((v) => !v)} />
    </aside>
  );
}
