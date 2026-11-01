import { Link } from 'react-router-dom';
import { MachineCard } from '@/components/monitor/machine-card';
import type { Machine, MonitorSystem } from '@/lib/types';

/**
 * C8 / D2：总控台卡片的 widget 化外壳。
 *
 * 这里只做一件事——把「静态渲染树」和「网格渲染树」都要用的那几块拆出来，
 * 两条路径共用同一份 JSX。**输出 DOM 与拆分前逐字一致**，
 * 因为 D2 的验收要求是「无 override 时与现状像素级一致」。
 * （原 InspectionToolbar 已随 D4-2.3 收进顶栏，见 inspection-actions.tsx。）
 */

export interface ServerMachineCardProps {
  /** Beszel 对应 system；台账有、Beszel 没有时不传，指标格整体退化为 — */
  system?: MonitorSystem;
  /** 台账机器；缺失表示这是个未登记的 Beszel 系统 */
  machine?: Machine;
}

/**
 * 服务器卡。B2-4：server 卡无内部交互，整卡可点进单机详情；hover 工具类加在 Link 上。
 * 未登记的 Beszel 系统（含 `/api/machines` 未就绪）就着 monitor 字段现合一个最小 Machine：
 * 台账格按 §4.3 逐格退化，卡片骨架仍是同一套 MachineCard。没有台账 id 就没有单机页可去，
 * 所以那一支不包 Link。
 */
export function ServerMachineCard({ system, machine }: ServerMachineCardProps) {
  if (!machine) {
    if (!system) return null;
    return (
      <MachineCard
        machine={{ id: system.id, label: system.name, role: 'server', public_host: system.host }}
        system={system}
      />
    );
  }
  return (
    <Link
      to={`/devices/${machine.id}`}
      className="block rounded-lg transition-all hover:ring-2 hover:ring-primary/30 focus-visible:ring-2"
    >
      <MachineCard machine={machine} system={system} />
    </Link>
  );
}

export interface NodeMachineCardProps {
  machine: Machine;
  system?: MonitorSystem;
  onOpen: () => void;
  onActionDone: () => void;
}

/**
 * 节点卡。用 onClick 而不是包 `<Link>`：卡内动作按钮的 stopPropagation 挡得住合成事件冒泡，
 * 却挡不住原生 `<a>` 的默认导航——包 Link 会让「重启 sing-box」按钮点下去整页跳转。
 */
export function NodeMachineCard({ machine, system, onOpen, onActionDone }: NodeMachineCardProps) {
  return (
    <MachineCard
      machine={machine}
      system={system}
      onClick={onOpen}
      onActionDone={onActionDone}
    />
  );
}
