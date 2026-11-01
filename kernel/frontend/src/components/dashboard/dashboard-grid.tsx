import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react';
import type { ComponentType, ReactNode } from 'react';
import { EllipsisVertical } from 'lucide-react';
import { GridStack, useGridStack, useGridStackItem } from 'gridstack/dist/react';
import type {
  ComponentMap,
  GridStackHandle,
  GridStackNode,
  GridStackOptions,
} from 'gridstack/dist/react';
import 'gridstack/dist/gridstack.min.css';
import './dashboard-grid.css';
import { MiniMenu } from './mini-menu';
import { GRID_COLUMNS, minWidthOf, serializeLayout } from './layout-model';
import type { LayoutItem, WidgetKey } from './layout-model';

/**
 * C8 查看态（D2）+ 编辑态（D3）渲染器：GridStack 13 官方 React wrapper
 * （`gridstack/dist/react`）。`staticGrid` 一个开关切两态——查看态只渲染不拖不缩，
 * 编辑态解除 static 并给每张卡盖上交互屏蔽层。
 *
 * 引擎选型与三条验收点见 `spike/dashboard-engine/`（S0' 复跑）与提案 C8 小节。
 */

/** 小 cell 是「内容自适应高度不裁切」的前提：W4 的视觉裁切就是 cellHeight=80 取整粗粒度。 */
const CELL_HEIGHT = 10;
/** 每侧 8px → 卡间距 16px，与静态渲染树的 `gap-4` 对齐。 */
const MARGIN = 8;

/**
 * 断点用引擎内置的 `columnOpts`，不写自定义装箱。
 * 数组按大→小匹配（`w <= breakpoints[i].w` 命中）：<768 → 1 列线性，768–1279 → 6 列，≥1280 → 12 列。
 * `breakpointForWindow` 取视口宽而不是网格宽——现状 `md:` / `xl:` 也是视口口径，换成网格宽会和边栏折叠状态耦合。
 */
const BREAKPOINTS = [
  { w: 1279, c: 6 },
  { w: 767, c: 1, layout: 'list' as const },
];

type WidgetRenderer = (key: WidgetKey) => ReactNode;

const RendererContext = createContext<WidgetRenderer>(() => null);

interface EditContextValue {
  editing: boolean;
  onHideWidget?: (key: WidgetKey) => void;
  labelOf?: (key: WidgetKey) => string;
}

const EditContext = createContext<EditContextValue>({ editing: false });

/**
 * 引擎节点 → 存储契约的 `{key,x,y,w}`。
 *
 * `h` 不取（由 `resizeToContent` 按内容现算），`id` 就是 widget key。
 * 坐标缺失的节点直接丢——宁可少存一行让它下次按 default 追加到末尾，
 * 也不要往 override 里写一条 `parseLayout` 会当坏数据丢掉的记录。
 */
export function layoutFromNodes(nodes: readonly GridStackNode[]): LayoutItem[] {
  const items: LayoutItem[] = [];
  for (const node of nodes) {
    const key = typeof node.id === 'string' ? node.id : null;
    if (!key) continue;
    const { x, y, w } = node;
    if (typeof x !== 'number' || typeof y !== 'number' || typeof w !== 'number') continue;
    items.push({ key, x, y, w });
  }
  return items;
}

/**
 * 一个格子的内容宿主。
 *
 * 三件事：
 * 1. 从 context 拿渲染函数——widget JSON 的 `props` 会被 wrapper 拿去做 options 签名
 *    （`JSON.stringify`），所以只能放可序列化的 `widgetKey`，实时数据走 context。
 * 2. 内容渲染出来之后再喊 `resizeToContent`。**不能**在 grid options 里设 `sizeToContent`：
 *    wrapper 用 portal 渲染内容，而 GridStack 在 init/load 阶段就会对每个节点跑一遍
 *    `resizeToContent`，那时 `.grid-stack-item-content` 还是空的，只会刷一屏
 *    `firstElementChild is null`（S0' 实测）。
 * 3. 编辑态盖一层交互屏蔽（D3）：机器卡整卡是 `<Link>`、节点卡带重启确认框，
 *    拖拽结束的那次 click 会直接把人送进单机页或弹出确认框。屏蔽层是卡片的**兄弟节点**
 *    而不是祖先，所以点击落在它身上时既不会冒泡进 `<a>`、也不会触发卡内 `onClick`；
 *    它本身是 `div`，不在 gridstack 的 `skipMouseDown`（`input,textarea,button,…`）名单里，
 *    mousedown 照常冒泡到 `.grid-stack-item-content` 起拖。
 */
function WidgetHost({ widgetKey }: { widgetKey: string }) {
  const render = useContext(RendererContext);
  const { editing, onHideWidget, labelOf } = useContext(EditContext);
  const { grid } = useGridStack();
  const { node } = useGridStackItem();
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = node?.el;
    const box = boxRef.current;
    if (!grid || !el || !box) return;
    let raf = 0;
    const sync = () => {
      grid.resizeToContent(el);
      /*
       * 必须跑第二遍。`resizeToContent` 里的 `padding = 外框高 − 内容盒高`，
       * 第一遍跑的时候格子还是初始高度（内容盒被压到 0），padding 量成 10 而不是 16，
       * 算出来的 h 稳定差一格——实测卡片比格子高 5px。第二遍在已经正确的盒子上重算就到位；
       * 到位之后 `itemH === wantedH` 会让它 early-return，不会来回跳。
       */
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => grid.resizeToContent(el));
    };
    const observer = new ResizeObserver(sync);
    observer.observe(box);
    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [grid, node]);

  const name = labelOf?.(widgetKey) ?? widgetKey;

  return (
    <div ref={boxRef} className={editing ? 'relative' : undefined}>
      {render(widgetKey)}
      {editing && (
        <>
          <div
            aria-hidden="true"
            data-widget-shield={widgetKey}
            className="absolute inset-0 z-10 cursor-move rounded-lg"
            onClick={(e) => {
              // 屏蔽层已经把 click 挡在卡片外面了，这里再按死一次：
              // 它自己是格子的一部分，任何冒泡出去的语义都不该有。
              e.preventDefault();
              e.stopPropagation();
            }}
          />
          <div className="absolute right-1 top-1 z-20">
            <MiniMenu
              label={`${name} 的布局菜单`}
              trigger={<EllipsisVertical className="h-3.5 w-3.5" />}
              triggerClassName="h-7 w-7 px-0"
              items={
                onHideWidget
                  ? [{ key: 'hide', label: '隐藏', onSelect: () => onHideWidget(widgetKey) }]
                  : []
              }
            />
          </div>
        </>
      )}
    </div>
  );
}

// 模块级常量：`components` 进了 wrapper 内部 useMemo 的依赖，行内新建会让每次渲染都重算 portal。
const COMPONENTS: ComponentMap = {
  W: WidgetHost as ComponentType<Record<string, unknown>>,
};

export interface DashboardGridProps {
  /** 已经 `default ⊕ override` 合并好的布局 */
  layout: LayoutItem[];
  /** key → 卡片。调用方持有实时数据，网格只管定位 */
  renderWidget: WidgetRenderer;
  /** D3 编辑态：解除 staticGrid、盖交互屏蔽层、出 ⋮ 菜单 */
  editing?: boolean;
  /** 拖拽 / 缩放结束后的整份 12 列布局（只在 12 列档回调） */
  onLayoutChange?: (items: LayoutItem[]) => void;
  /** ⋮ 菜单里的「隐藏」 */
  onHideWidget?: (key: WidgetKey) => void;
  /** key → 中文名，⋮ 菜单的无障碍名用 */
  labelOf?: (key: WidgetKey) => string;
}

export function DashboardGrid({
  layout,
  renderWidget,
  editing = false,
  onLayoutChange,
  onHideWidget,
  labelOf,
}: DashboardGridProps) {
  // 布局本身变了才重建网格（机器增删、override 变化）。指标刷新不会改这个串。
  const signature = useMemo(() => serializeLayout(layout), [layout]);
  const gridRef = useRef<GridStackHandle>(null);

  /**
   * **`editing` 绝对不能进 options**（浏览器实测踩过）：wrapper 发现 options 签名变了就调
   * `grid.updateOptions()`，而它最后一句是 `if (o.children?.length) this.load(o.children)`——
   * 我们的 children 按 C8 契约不带 `h`，一 load 全部节点高度打回 1 格、`float:false` 再把
   * y 压成 0..n，整页卡片瞬间塌成一条条 10px；`resizeToContent` 又不会自己回来
   * （每张卡的 `ResizeObserver` 盯的是内容盒，内容盒尺寸没变，观察者不触发）。
   * 所以 options 只跟 `layout` 走，static 与否改由下面的 effect 用 `setStatic` 直接切。
   */
  const options = useMemo<GridStackOptions>(
    () => ({
      cellHeight: CELL_HEIGHT,
      margin: MARGIN,
      float: false,
      staticGrid: true, // 初值 = 查看态；编辑态由 setStatic 接手
      columnOpts: {
        columnMax: GRID_COLUMNS,
        breakpointForWindow: true,
        breakpoints: BREAKPOINTS,
      },
      children: layout.map((item) => ({
        id: item.key,
        x: item.x,
        y: item.y,
        w: item.w,
        minW: minWidthOf(item.key),
        component: 'W',
        props: { widgetKey: item.key },
      })),
    }),
    [layout]
  );

  // 查看态 ⇄ 编辑态就这一行。`signature` 进依赖是因为布局变化会整块重建网格，
  // 新实例按 options 起来是 static 的，得把编辑态重新按上去。
  useEffect(() => {
    gridRef.current?.getGrid()?.setStatic(!editing);
  }, [editing, signature]);

  /**
   * 只在**用户手势结束**时回写，不挂 `change`：
   * `resizeToContent` 与 init 期的 `float:false` 压实都会触发 `change`，
   * 挂在那上面等于「进一次编辑态就凭空生成一份 override」，
   * 直接毁掉「没设过 override = 与旧版像素级一致」这条契约。
   *
   * 另外只认 12 列档：6 列 / 1 列是引擎按断点现算的，存进去就是往 12 列契约里塞脏数据。
   */
  const readLayout = useCallback(() => {
    const grid = gridRef.current?.getGrid();
    if (!grid || grid.getColumn() !== GRID_COLUMNS) return null;
    return layoutFromNodes(grid.engine.nodes);
  }, []);

  /**
   * 手势起点快照。**没挪动的手势一律不回写**：GridStack 的起拖阈值是 3px，
   * 抖一下就够触发一次 `dragstop`，那时布局跟按下去之前一模一样——
   * 照写的话，一次误触就把从没自定义过的用户永久推进网格路径（分区标题就此消失）。
   */
  const gestureStartRef = useRef<string | null>(null);

  const beginGesture = useCallback(() => {
    const items = readLayout();
    gestureStartRef.current = items ? serializeLayout(items) : null;
  }, [readLayout]);

  const endGesture = useCallback(() => {
    if (!onLayoutChange) return;
    const items = readLayout();
    if (!items) return;
    const next = serializeLayout(items);
    if (next === gestureStartRef.current) return;
    gestureStartRef.current = next;
    onLayoutChange(items);
  }, [onLayoutChange, readLayout]);

  const editContext = useMemo<EditContextValue>(
    () => ({ editing, onHideWidget, labelOf }),
    [editing, onHideWidget, labelOf]
  );

  return (
    <RendererContext.Provider value={renderWidget}>
      <EditContext.Provider value={editContext}>
        {/* key 挂布局签名：布局变了整块重建，不依赖 wrapper 的 updateOptions 语义。
            编辑态开关不进这个 key——它走 options→setStatic，重建反而会打断编辑。 */}
        <GridStack
          key={signature}
          ref={gridRef}
          className={
            editing
              ? 'console-dashboard-grid is-editing grid-stack'
              : 'console-dashboard-grid grid-stack'
          }
          options={options}
          components={COMPONENTS}
          onDragStart={beginGesture}
          onDragStop={endGesture}
          onResizeStart={beginGesture}
          onResizeStop={endGesture}
        />
      </EditContext.Provider>
    </RendererContext.Provider>
  );
}
