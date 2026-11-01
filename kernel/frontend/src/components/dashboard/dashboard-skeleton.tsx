import { Skeleton } from '@/components/ui/skeleton';

/**
 * C8-1：总控台首屏骨架。
 *
 * 静态渲染树删除后总控台只剩网格一条路径，而网格的格子集合要等
 * **布局（`GET /api/dashboard-layout`）+ 台账（`/api/machines`）+ Beszel（`/api/monitor`）**
 * 三份数据都到位才算定型——数据没齐就渲染网格，机器卡会先缺席再补进来，
 * 布局签名一变整块网格重建（见 `dashboard-grid.tsx` 的 `key={signature}`），首屏会闪。
 * 所以三份数据到位前一律显示本骨架；**三份都有「失败也置位」的出口**，
 * 任何一份挂掉都不会把页面卡在骨架上（失败 → 默认布局只读渲染）。
 *
 * 形状按默认布局的顺序摆：今日关注 → 监控事件 → 机器卡区（一行四张）→ 运维状态 / tailnet。
 */

/**
 * 机器卡骨架的兜底列数。台账先到时按真实 server 台数铺（I0-4），
 * 台账也没到就用这个常量——现状 4 台 server（nas/txy/aly/us）。
 */
export const SERVER_SKELETON_COUNT = 4;

interface Props {
  /** 机器卡骨架数量；台账未到时用兜底常量 */
  serverCount?: number;
}

export function DashboardSkeleton({ serverCount = SERVER_SKELETON_COUNT }: Props) {
  return (
    <div className="space-y-6" role="status" aria-label="总控台加载中">
      {/* 今日关注 */}
      <Skeleton className="h-16 w-full" />
      {/* 监控事件 */}
      <Skeleton className="h-24 w-full" />
      {/* 机器卡区：与网格 12 列档的一行四张、静态树时代的 xl:grid-cols-4 都对得上 */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: Math.max(1, serverCount) }).map((_, i) => (
          <Skeleton key={i} className="h-40 w-full" />
        ))}
      </div>
      {/* 运维状态 / tailnet / 入口流量 */}
      <Skeleton className="h-28 w-full" />
      <Skeleton className="h-28 w-full" />
    </div>
  );
}
