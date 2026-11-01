import { useSyncExternalStore } from 'react';
import { ClipboardCopy, Download, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';

/**
 * C8 / D4-2.3：把总控台正文区的「生成巡检报告」工具条收进顶栏，释放一整行。
 *
 * 顶栏是全局外壳（App.tsx 持有），而巡检报告的 busy 状态与动作处理都在
 * 总控台路由里——接线方式沿用 D3 编辑态验证过的模块级订阅源
 * （edit-mode.tsx 同款）：总控台挂载时登记控制器，顶栏订阅它。
 * 未登记（其它页面 / 路由未挂载）时顶栏什么都不渲染，`App.tsx` 零改动。
 *
 * 只读聚合，点它不会触发任何探测（与原 InspectionToolbar 同契约）。
 */

export interface InspectionActionsController {
  /** 同时只允许一个动作在跑：两次点击会各自向后端要一份报告，采样时刻还不一样 */
  busy: 'copy' | 'download' | null;
  onRun: (mode: 'copy' | 'download') => void;
}

let current: InspectionActionsController | null = null;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot() {
  return current;
}

export function setInspectionActionsController(next: InspectionActionsController | null) {
  if (current === next) return;
  current = next;
  emit();
}

/** 卸载时只撤销自己登记的那个，避免 StrictMode 双挂载误清后来者。 */
export function clearInspectionActionsController(owned: InspectionActionsController) {
  if (current !== owned) return;
  current = null;
  emit();
}

export function useInspectionActionsController(): InspectionActionsController | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** 顶栏里的两个巡检报告动作。控制器未登记时整块不渲染。 */
export function InspectionActionsControls() {
  const controller = useInspectionActionsController();
  if (!controller) return null;

  const { busy, onRun } = controller;

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="h-[44px] w-[44px] md:h-8 md:w-8"
        disabled={busy !== null}
        onClick={() => onRun('copy')}
        aria-label="复制巡检报告"
        title="复制巡检报告到剪贴板"
      >
        {busy === 'copy' ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          <ClipboardCopy className="h-4 w-4" />
        )}
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-[44px] w-[44px] md:h-8 md:w-8"
        disabled={busy !== null}
        onClick={() => onRun('download')}
        aria-label="下载巡检报告"
        title="下载巡检报告 .md"
      >
        {busy === 'download' ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          <Download className="h-4 w-4" />
        )}
      </Button>
    </>
  );
}
