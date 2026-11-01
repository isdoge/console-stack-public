import { useSyncExternalStore } from 'react';
import { Columns3, Plus, Settings } from 'lucide-react';
import { Button } from '@/components/ui/button';

export interface DeviceActionsController {
  onAdd: () => void;
  onCompare?: () => void;
  onEdit?: () => void;
}

// 沿用巡检报告的顶栏订阅方式；弹窗及数据仍由设备路由持有。
let current: DeviceActionsController | null = null;
const listeners = new Set<() => void>();
function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
function getSnapshot() { return current; }
export function setDeviceActionsController(next: DeviceActionsController) {
  current = next;
  for (const listener of listeners) listener();
}
export function clearDeviceActionsController(owned: DeviceActionsController) {
  if (current !== owned) return;
  current = null;
  for (const listener of listeners) listener();
}

export function DeviceActionsControls() {
  const controller = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  if (!controller) return null;
  return (
    <>
      {controller.onCompare && (
        <Button variant="ghost" size="sm" className="h-[30px] gap-1 px-2" aria-label="对比" title="对比机器" onClick={controller.onCompare}>
          <Columns3 className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">对比</span>
        </Button>
      )}
      {controller.onEdit && (
        <Button variant="ghost" size="sm" className="h-[30px] gap-1 px-2" aria-label="编辑本机" title="编辑本机" onClick={controller.onEdit}>
          <Settings className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">编辑本机</span>
        </Button>
      )}
      <Button variant="outline" size="sm" className="h-[30px] gap-1 px-2" aria-label="机器" title="添加机器" onClick={controller.onAdd}>
        <Plus className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">机器</span>
      </Button>
    </>
  );
}
