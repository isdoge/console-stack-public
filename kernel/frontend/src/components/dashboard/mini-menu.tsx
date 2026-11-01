import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

/**
 * C8 / D3 编辑态用的轻量下拉菜单。
 *
 * **为什么不用 `components/ui/dropdown-menu`（Radix）**：编辑态的两处菜单都要写断言
 * （卡上「隐藏」、顶栏「添加 widget」），而 Radix 的 DismissableLayer 在 jsdom 里依赖
 * `hasPointerCapture` / PointerEvent，本仓库现有测试从没跑过它，为两个单项菜单去给
 * `setupTests.ts` 加全局垫片不划算（那也是别的 Track 会碰的公共文件）。
 * 这里只要「点开 → 选一项 → 关掉」，二十来行原生实现就够，且完全可断言。
 */

export interface MiniMenuItem {
  key: string;
  label: string;
  onSelect: () => void;
}

export interface MiniMenuProps {
  /** 触发按钮里的内容（图标 / 文案） */
  trigger: ReactNode;
  /** 触发按钮的无障碍名，也是测试里找它的抓手 */
  label: string;
  items: MiniMenuItem[];
  /** items 为空时菜单里显示的说明 */
  emptyText?: string;
  className?: string;
  triggerClassName?: string;
  /** 面板对齐方向，默认贴右 */
  align?: 'start' | 'end';
}

export function MiniMenu({
  trigger,
  label,
  items,
  emptyText = '没有可选项',
  className,
  triggerClassName,
  align = 'end',
}: MiniMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={cn('relative', className)}>
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        title={label}
        className={cn(
          'inline-flex h-7 items-center justify-center gap-1 rounded-md border bg-background/90 px-2',
          'text-xs text-foreground shadow-sm backdrop-blur transition-colors hover:bg-accent',
          triggerClassName
        )}
        onClick={() => setOpen((v) => !v)}
      >
        {trigger}
      </button>
      {open && (
        <div
          role="menu"
          className={cn(
            'absolute top-full z-30 mt-1 min-w-[9rem] overflow-hidden rounded-md border bg-popover p-1 shadow-md',
            align === 'end' ? 'right-0' : 'left-0'
          )}
        >
          {items.length === 0 ? (
            <p className="px-2 py-1.5 text-xs text-muted-foreground">{emptyText}</p>
          ) : (
            items.map((item) => (
              <button
                key={item.key}
                type="button"
                role="menuitem"
                className="block w-full truncate rounded-sm px-2 py-1.5 text-left text-xs hover:bg-accent"
                onClick={() => {
                  setOpen(false);
                  item.onSelect();
                }}
              >
                {item.label}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
