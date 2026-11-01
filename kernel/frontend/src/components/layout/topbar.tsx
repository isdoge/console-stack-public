import { useEffect, useState, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { Menu, Moon, RefreshCw, Search, Sun } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { SidebarContent } from '@/components/layout/sidebar';
import { DashboardEditControls, useDashboardEditing } from '@/components/dashboard/edit-mode';
import { InspectionActionsControls } from '@/components/dashboard/inspection-actions';
import { DeviceActionsControls } from '@/components/monitor/device-actions';
import { useTheme } from '@/lib/use-theme';
import { pluginForPath } from '@/lib/plugins/registry';
import { cn } from '@/lib/utils';

const TITLES: Record<string, string> = {
  '/': '总控台',
  '/devices': '设备',
  '/settings': '设置',
};

interface Props {
  onRefresh?: () => void;
  refreshing?: boolean;
  /** T12 分支：打开全局命令面板（Ctrl/Cmd+K 同效） */
  onOpenPalette?: () => void;
  panelSessions?: ReactNode;
}

export function Topbar({ onRefresh, refreshing, onOpenPalette, panelSessions }: Props) {
  const loc = useLocation();
  const [mobileOpen, setMobileOpen] = useState(false);
  // T12：亮/暗主题切换（偏好持久 localStorage，system 跟随系统）
  const { resolved, toggle } = useTheme();
  // C8 / D3：总控台布局编辑态期间禁手动刷新——`refreshKey` 一变卡片整片重渲染，手势直接断
  const editingLayout = useDashboardEditing();
  const isDevices = loc.pathname === '/devices' || loc.pathname.startsWith('/devices/');
  // /devices/:id 单机视图沿用「设备」标题；/panel/* 单独处理
  const title = loc.pathname.startsWith('/panel/')
    ? '外部面板'
    : pluginForPath(loc.pathname)?.name ?? TITLES[loc.pathname] ?? (loc.pathname.startsWith('/devices/') ? '设备' : '页面');

  // 浏览器标签页标题跟随路由（面板页由 PanelHost 设置）
  useEffect(() => {
    document.title = `${title} · 自托管运维总控台`;
  }, [title]);

  return (
    // sticky：让正文从毛玻璃底下穿过（否则 blur 背后无移动内容，等于 no-op），
    // 同时拿到长页面里标准的常驻顶栏行为（E 遗留项 5）
    <header className={cn('sticky top-0 z-20 flex min-h-14 flex-wrap items-center justify-between gap-x-2 border-b glass-panel px-2 py-1 md:px-4', isDevices && 'max-sm:flex-nowrap max-sm:[&_button]:h-[30px] max-sm:[&_button]:min-w-[30px] max-sm:[&_button]:w-auto')}>
      <div className={cn('flex min-h-[44px] shrink-0 items-center gap-2', isDevices && 'max-sm:min-h-[30px]')}>
        {/* 窄屏：汉堡按钮弹抽屉导航（桌面侧栏 hidden 时唯一入口） */}
        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetTrigger asChild>
            <Button variant="ghost" size="icon" className="h-[44px] w-[44px] md:hidden" aria-label="打开导航">
              <Menu className="h-4 w-4" />
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="flex w-64 flex-col p-0">
            <SheetTitle className="sr-only">导航</SheetTitle>
            <SidebarContent onNavigate={() => setMobileOpen(false)} />
          </SheetContent>
        </Sheet>
        <h1 className="text-base font-semibold">{title}</h1>
      </div>
      <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-1 md:gap-2">
        {isDevices && <DeviceActionsControls />}
        {panelSessions}
        {onOpenPalette && (
          <Button
            variant="ghost"
            size="sm"
            className="h-[44px] min-w-[44px] gap-1.5 px-2 text-xs text-muted-foreground md:h-8 md:min-w-0"
            onClick={onOpenPalette}
            title="快速跳转（Ctrl+K）"
            aria-label="快速跳转"
          >
            <Search className="h-3.5 w-3.5" />
            <kbd className="hidden rounded bg-muted px-1 py-0.5 text-[10px] sm:inline">Ctrl K</kbd>
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon"
          className="h-[44px] w-[44px] md:h-8 md:w-8"
          onClick={toggle}
          aria-label={resolved === 'dark' ? '切换到亮色' : '切换到暗色'}
          title={resolved === 'dark' ? '切换到亮色' : '切换到暗色'}
        >
          {resolved === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </Button>
        {/* D4-2.3：总控台挂载时登记巡检报告动作，其它页面为空（正文区工具条已撤，释放一整行） */}
        <InspectionActionsControls />
        {/* 总控台在编辑布局时才渲染内容，其余页面/窄屏为空 */}
        <DashboardEditControls />
        {onRefresh && (
          <Button
            variant="outline"
            size="sm"
            className="h-[44px] min-w-[44px] px-2 md:h-9 md:px-3"
            aria-label="刷新"
            onClick={onRefresh}
            disabled={refreshing || editingLayout}
            title={editingLayout ? '布局编辑中已暂停刷新' : undefined}
          >
            <RefreshCw className={refreshing ? 'h-4 w-4 animate-spin md:mr-2' : 'h-4 w-4 md:mr-2'} />
            <span className="hidden md:inline">刷新</span>
          </Button>
        )}
      </div>
    </header>
  );
}
