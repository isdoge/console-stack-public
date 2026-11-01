/**
 * D3：全局背景层。
 *
 * fixed 全屏、纯 CSS 渐变 mesh（青蓝/淡紫/薄荷系，参考 Glassmorphism 默认背景观感），
 * 挂在 App 最底层（-z-10）。body 保持透明，玻璃卡（.glass-surface）透过它呈现磨砂观感；
 * html 保留降级底色（index.css），本组件未渲染 / 样式失效时兜底。
 * 渐变本体放在 index.css（.bg-mesh / .dark .bg-mesh），暗色换深空底色。
 */
export function Background() {
  return <div aria-hidden className="bg-mesh pointer-events-none fixed inset-0 -z-10" />;
}
