import { useEffect, useState, useSyncExternalStore } from 'react';
import { Check, Pencil, Plus, RotateCcw, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { MiniMenu } from '@/components/dashboard/mini-menu';
import type { WidgetKey } from '@/components/dashboard/layout-model';

/**
 * C8 / D3：总控台布局编辑态的「顶栏 ↔ 路由」接线。
 *
 * 铅笔按钮按方案要求长在顶栏上，而 `refreshKey` 与 `<Topbar>` 都归 `App.tsx` 持有——
 * 常规做法是把编辑态状态提到 `App.tsx` 再往两边传。这里没那么做：`App.tsx` 是并行 Track
 * 都可能改的公共外壳，本 Track 的独占文件表里也没有它。改用一个模块级订阅源：
 * 总控台挂载时登记控制器，顶栏订阅它——顶栏的改动因此收敛成「渲染一个组件 + 刷新按钮多一个
 * disabled 条件」，`App.tsx` 零改动。
 *
 * 控制器为 null 时顶栏什么都不渲染，所以「只有总控台、且视口够宽才有编辑入口」
 * 这条规则由总控台单方面决定（见 `useLayoutEditable`）。
 *
 * D1' 改「编辑会话制」：拖拽/显隐/重置都暂存内存，铅笔只负责进入，
 * 出口只有两个——「保存」（PUT 到 DB）与「放弃」（丢弃回读 DB）。
 */

export interface HiddenWidgetOption {
  key: WidgetKey;
  label: string;
}

export interface DashboardEditController {
  editing: boolean;
  /** 进入编辑态（出口只有 save / cancel，铅笔不再是开关） */
  enter: () => void;
  /** 保存本轮编辑（PUT 整体替换）；saving 期间按钮禁用 */
  save: () => void;
  /** 放弃本轮编辑，回读 DB 最近一次保存的状态 */
  cancel: () => void;
  /** 保存请求在途 */
  saving: boolean;
  /** 清掉 override 与隐藏集合，回到代码现算的默认布局（暂存，保存才落库） */
  reset: () => void;
  /** 当前被隐藏的 widget（顶栏「添加 widget」列表） */
  hidden: HiddenWidgetOption[];
  /** 找回一张隐藏的卡 */
  show: (key: WidgetKey) => void;
}

let current: DashboardEditController | null = null;
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

export function setDashboardEditController(next: DashboardEditController | null) {
  if (current === next) return;
  current = next;
  emit();
}

/** 卸载时只撤销自己登记的那个，避免 StrictMode 双挂载把后来者清掉。 */
export function clearDashboardEditController(owned: DashboardEditController) {
  if (current !== owned) return;
  current = null;
  emit();
}

export function useDashboardEditController(): DashboardEditController | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** 顶栏刷新按钮用：编辑态期间 `refreshKey` 变化会整片重渲染卡片，直接打断手势。 */
export function useDashboardEditing(): boolean {
  return useDashboardEditController()?.editing ?? false;
}

/**
 * 编辑态的可用下限 = 12 列档。
 *
 * `dashboard-grid.tsx` 的 `columnOpts.breakpoints`（`breakpointForWindow: true`）是
 * ≥1280 → 12 列、768–1279 → 6 列、<768 → 1 列线性，而 C8 的存储契约**只持久化 12 列坐标**。
 * 在 6 列档拖出来的 `x/w` 没法忠实还原成 12 列（引擎的 `moveScale` 换算来回都要取整，
 * w=3 出去回来变 4），写进 override 就是往 12 列契约里塞脏数据；1 列档引擎本就 static 禁拖。
 * 所以窄于 1280 一律不给编辑入口——按方案「隐藏或禁用二选一」选**隐藏**（不登记控制器，
 * 顶栏连按钮都不渲染），比给一个点不动的按钮更好解释。
 */
export const EDIT_MIN_WIDTH = 1280;

export function useLayoutEditable(): boolean {
  const [editable, setEditable] = useState(
    () => typeof window === 'undefined' || window.innerWidth >= EDIT_MIN_WIDTH
  );
  useEffect(() => {
    const sync = () => setEditable(window.innerWidth >= EDIT_MIN_WIDTH);
    sync();
    window.addEventListener('resize', sync);
    return () => window.removeEventListener('resize', sync);
  }, []);
  return editable;
}

/** 顶栏里的编辑态入口。没有总控台登记控制器时整块不渲染。 */
export function DashboardEditControls() {
  const controller = useDashboardEditController();
  if (!controller) return null;

  const { editing, enter, save, cancel, saving, reset, hidden, show } = controller;

  if (!editing) {
    return (
      <Button
        variant="ghost"
        size="icon"
        className="h-8 w-8"
        onClick={enter}
        aria-label="编辑布局"
        title="编辑布局"
      >
        <Pencil className="h-4 w-4" />
      </Button>
    );
  }

  return (
    <>
      <MiniMenu
        label="添加 widget"
        trigger={
          <>
            <Plus className="h-3.5 w-3.5" />
            添加 widget
          </>
        }
        items={hidden.map((item) => ({
          key: item.key,
          label: item.label,
          onSelect: () => show(item.key),
        }))}
        emptyText="没有被隐藏的卡片"
      />
      <Button variant="ghost" size="sm" className="h-8 gap-1.5 px-2 text-xs" onClick={reset}>
        <RotateCcw className="h-3.5 w-3.5" />
        重置默认布局
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-8 w-8"
        onClick={cancel}
        disabled={saving}
        aria-label="放弃布局修改"
        title="放弃修改"
      >
        <X className="h-4 w-4" />
      </Button>
      <Button
        variant="default"
        size="icon"
        className="h-8 w-8"
        onClick={save}
        disabled={saving}
        aria-label="保存布局"
        title="保存布局"
      >
        <Check className="h-4 w-4" />
      </Button>
    </>
  );
}
