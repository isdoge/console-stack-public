import { useState } from 'react';
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  Coins,
  Cpu,
  HardDrive,
  MemoryStick,
  MonitorSmartphone,
  RotateCw,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ProbeBand, ProbeBandRow } from '@/components/monitor/probe-band';
import { runMachineAction,
  apiErrorMessage,
} from '@/lib/api';
import { cn } from '@/lib/utils';
import { isActiveRenewal, RENEWAL_STATUS_LABELS } from '@/lib/renewal-status';
import type {
  Machine,
  MachineAction,
  MachineRenewal,
  MachineTraffic,
  MonitorSystem,
  ProbeSample,
} from '@/lib/types';

// ---------------------------------------------------------------------------
// 格式化与算法
// ---------------------------------------------------------------------------

/** D2/§4.3：用量色带，90/70 阈值 → signal-5/4/1（越高越接近告警红）。 */
export function meterTone(pct: number) {
  if (pct >= 90) return 'bg-signal-5';
  if (pct >= 70) return 'bg-signal-4';
  return 'bg-signal-1';
}

/** 瞬时速率。参考图口径：B/s → K/s → M/s，保 1 位小数。 */
export function formatRate(bytesPerSec?: number | null) {
  if (bytesPerSec == null) return '—';
  if (bytesPerSec >= 1024 * 1024) return `${(bytesPerSec / 1024 / 1024).toFixed(1)} M/s`;
  if (bytesPerSec >= 1024) return `${(bytesPerSec / 1024).toFixed(1)} K/s`;
  return `${Math.round(bytesPerSec)} B/s`;
}

/**
 * 内存副行「已用 / 总量」（§4.1 kr 用 MB、txy 用 GB）。
 * **单位由总量决定并施加于两侧**：per-value 换算会出 `862.4 MB / 1.9 GB` 这种
 * 混单位行，读起来要在心里换算才知道用了多少，§4.1/§4.2 两侧单位都是一致的。
 */
export function formatMbPair(used?: number | null, total?: number | null): string | undefined {
  if (used == null || total == null) return undefined;
  if (total >= 1024) return `${(used / 1024).toFixed(1)} GB / ${(total / 1024).toFixed(1)} GB`;
  return `${used.toFixed(1)} MB / ${total.toFixed(1)} MB`;
}

export function formatGb(gb?: number | null) {
  if (gb == null) return '—';
  return `${gb.toFixed(1)} GB`;
}

/** 在线天数 chip。不足一天显示小时，免得新装机器恒显「在线 0 天」。 */
export function uptimeText(sec?: number | null): string | null {
  if (sec == null) return null;
  const days = Math.floor(sec / 86400);
  if (days >= 1) return `在线 ${days} 天`;
  const hours = Math.floor(sec / 3600);
  if (hours >= 1) return `在线 ${hours} 小时`;
  return `在线 ${Math.max(1, Math.floor(sec / 60))} 分钟`;
}

/** §3.3 周期天数。topup / onetime 没有周期，不摊日单价。 */
const CYCLE_DAYS: Record<string, number> = {
  monthly: 30,
  quarterly: 91,
  yearly: 365,
  biennial: 730,
  triennial: 1095,
};

const CYCLE_LABEL: Record<string, string> = {
  monthly: '月',
  quarterly: '季',
  yearly: '年',
  biennial: '2 年',
  triennial: '3 年',
  onetime: '一次性',
  topup: '充值',
};

/**
 * 价格 chip 文案（§4.3）。
 * - `cost` 缺失 → null，chip 整个不渲染（不知道价格 ≠ 免费）
 * - `cost === 0` → 「免费」（owner 已确认 kr/sg/us 免费，§3.2）
 */
/** 币种符号。认不出的币种回落成「金额 + 代码」，不硬当人民币。 */
const CURRENCY_SYMBOL: Record<string, string> = { USD: '$', CNY: '¥' };

/**
 * 给金额加币种，价格 chip 与剩余价值共用——两处各写一份会漂（实测 USD 机器价格显示
 * `$5 / 月`，剩余价值却显示 `¥0.50`）。
 * 收已格式化的数字串而不是 number：价格要原样显示（`69.99` 不能舍成 `70`），
 * 剩余价值要固定两位，小数位归调用方决定。
 */
export function moneyText(numText: string, currency?: string): string {
  const symbol = currency ? CURRENCY_SYMBOL[currency] : undefined;
  return symbol ? `${symbol}${numText}` : `${numText}${currency ? ` ${currency}` : ''}`;
}

/**
 * M1 月底预测的一句话（hover 用）。`projection` 为 null 时返回 null——**观测不足
 * 72 小时就什么都不说**，不要退化成「预计 0 GB」那种看着像真数字的假话。
 *
 * `partial_month` 必须说出来：账本从月中才起算时，这个预测是「按当前速率跑满
 * 一整月」的意思，跟云厂商账单对不上是正常的（设计 §5.7「首月明确标从首次采样起」）。
 */
export function projectionText(traffic?: MachineTraffic | null): string | null {
  const p = traffic?.projection;
  if (!p) return null;
  const head = `按 ${p.daily_gb} GB/天推算，本月预计 ${p.projected_gb} GB`;
  const quota = p.quota_gb ? ` / ${p.quota_gb} GB（${p.projected_pct}%）` : '';
  const partial = p.partial_month ? `；从首次采样 ${traffic?.since || '未知'} 起算，非整月` : '';
  return `${head}${quota}${partial}`;
}

export function priceText(renewal?: MachineRenewal | null): string | null {
  if (renewal?.cost == null) return null;
  if (renewal.cost === 0) return '免费';
  const amount = moneyText(String(renewal.cost), renewal.currency);
  const cycle = renewal.cycle ? CYCLE_LABEL[renewal.cycle] || renewal.cycle : null;
  return cycle ? `${amount} / ${cycle}` : amount;
}

/**
 * §3.3 剩余价值 = 日单价 × 剩余天数，日单价 = cost ÷ 周期天数。
 * 校验：¥69.99/年 + 剩余 180 天 → 34.5（参考图 ¥34.52）。
 * cost=0、无到期日、或周期不摊（onetime/topup）时返回 null。
 */
export function remainingValue(renewal?: MachineRenewal | null): number | null {
  if (!renewal?.cost || !isActiveRenewal(renewal) || renewal.days_left == null || renewal.days_left < 0) return null;
  const days = renewal.cycle ? CYCLE_DAYS[renewal.cycle] : undefined;
  if (!days) return null;
  return (renewal.cost / days) * renewal.days_left;
}

/**
 * 家宽公网探测的状态文案。up 给最近一次耗时；down「不通」；其余一律「未知」——
 * 包括 NAS 本机代理没放行（fake-ip / 握手快得不可能），那种结果说明不了节点通不通。
 */
export function publicProbeText(status?: string, samples?: ProbeSample[]): string {
  if (status === 'down') return '不通';
  if (status !== 'up') return '未知';
  const last = samples && samples.length > 0 ? samples[samples.length - 1] : undefined;
  return last?.status === 'up' && last.latency_ms != null ? `${last.latency_ms} ms` : '可达';
}

/** 状态点色：server 看 `sys.status`，node 看 `probe_status`（§4.3）。
 *  用 signal 语义色而非 green-500/red-500：后者对玻璃卡底只有 2.15:1，
 *  且状态点是有意义的非文本元素，参考线 3:1（D2 结论）。 */
export function statusTone(status?: string) {
  if (status === 'up') return 'bg-signal-1';
  if (status === 'down') return 'bg-signal-5';
  return 'bg-muted-foreground';
}

// ---------------------------------------------------------------------------
// 骨架零件
// ---------------------------------------------------------------------------

interface MetricCellProps {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  /** 右上角的主值；null 表示无数据，显示 — 且不画条 */
  pct?: number | null;
  /** 条下副行，如 "327.7 MB / 942.1 MB"；缺失时不渲染（不留空行） */
  sub?: string;
  hint?: string;
  /** 覆盖主值文案（流量格无配额时显示 — 而不是 0.0%） */
  valueText?: string;
  /**
   * 是否给副行留位置。由 MachineCard 按「行」统一决定：同行另一格有副行时，
   * 本格即使没数据也要留空占位，否则两格的条会错位；整行都没副行则整行不留，
   * 免得 Beszel 无数据的卡凭空多出四行死白（§4.3「副行不渲染」）。
   */
  subSlot?: boolean;
}

/**
 * 2×2 网格的一格（§4.1）：标签行 + 进度条 + 副行，纵向三层。
 * 纵排是被半宽格子逼出来的——D 那版横排定宽值列在这个宽度里放不下
 * （旧 `MetricRow` 已随 I0-2 删除）。
 */
function MetricCell({ icon: Icon, label, pct, sub, hint, valueText, subSlot }: MetricCellProps) {
  const value = valueText ?? (pct != null ? `${pct.toFixed(1)}%` : '—');
  return (
    <div className="flex flex-col gap-1" title={hint}>
      <div className="flex items-baseline justify-between text-xs">
        <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground">
          <Icon className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">{label}</span>
        </span>
        <span className="font-mono font-medium tabular-nums">{value}</span>
      </div>
      {/* 槽位恒在：四格的条对齐比「没数据就不画」重要，否则同卡内格子高度不齐。
          底色用 muted-foreground/20 而非 bg-muted：后者对玻璃卡底只有 1.10:1（Playwright 实测），
          CPU 0.2% / 流量 0.0% 这种近零值下填充也几乎没宽度，整格会读成空行——
          与 probe-band 空位格同一个坑、同一套值（改后 1.29:1）。 */}
      <div className="h-1 overflow-hidden rounded-full bg-muted-foreground/20">
        {pct != null && (
          <div
            className={cn('h-full rounded-full transition-all', meterTone(pct))}
            style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
          />
        )}
      </div>
      {subSlot && (
        // 纯占位（U+00A0）只为撑高，对读屏器屏掉；有真文案时照常朗读
        <span
          className="truncate text-[11px] leading-tight text-muted-foreground"
          aria-hidden={sub ? undefined : true}
        >
          {sub || ' '}
        </span>
      )}
    </div>
  );
}

interface DetailLine {
  icon: React.ComponentType<{ className?: string }>;
  text: string;
  tone?: string;
}

/** 三列细节行的一列（§4.1）：两行小字，图标 + 值。 */
function DetailColumn({ lines, hint }: { lines: DetailLine[]; hint?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-md bg-muted/40 px-2 py-1.5" title={hint}>
      {lines.map(({ icon: Icon, text, tone }, i) => (
        <div key={i} className={cn('flex items-center gap-1 text-[11px]', tone || 'text-muted-foreground')}>
          <Icon className="h-3 w-3 shrink-0" />
          <span className="truncate font-mono tabular-nums">{text}</span>
        </div>
      ))}
    </div>
  );
}

interface NodeActionsProps {
  machine: Machine;
  actions: MachineAction[];
  onDone?: () => void;
}

/** E3：白名单动作按钮。只送 action key，命令写死在后端 NODE_ACTIONS。 */
function NodeActions({ machine, actions, onDone }: NodeActionsProps) {
  const [pending, setPending] = useState<MachineAction | null>(null);
  const [running, setRunning] = useState(false);

  const run = async (action: MachineAction) => {
    setRunning(true);
    try {
      const res = await runMachineAction(machine.id, action.key);
      if (res.ok) {
        toast.success(`${machine.label}：${action.label}成功`);
        onDone?.();
      } else {
        toast.error(`${action.label}失败: ${res.output || res.error || ''}`);
      }
    } catch (err: unknown) {
      toast.error(`${action.label}失败: ${apiErrorMessage(err)}`);
    } finally {
      setRunning(false);
      setPending(null);
    }
  };

  return (
    <>
      <div className="flex shrink-0 flex-wrap gap-2">
        {actions.map((action) => (
          <Button
            key={action.key}
            variant="outline"
            size="sm"
            className="h-6 px-2 text-[11px]"
            disabled={running}
            // 整卡可点时（H 的 B2）不让动作按钮冒泡成跳转
            onClick={(e) => {
              e.stopPropagation();
              setPending(action);
            }}
          >
            <RotateCw className={cn('mr-1 h-3 w-3', running && 'animate-spin')} />
            {action.label}
          </Button>
        ))}
      </div>

      <AlertDialog open={pending != null} onOpenChange={(open) => !open && setPending(null)}>
        <AlertDialogContent onClick={(e) => e.stopPropagation()}>
          <AlertDialogHeader>
            <AlertDialogTitle>确认在 {machine.label} 上执行？</AlertDialogTitle>
            <AlertDialogDescription>
              将执行 <span className="font-mono">{pending?.label}</span>
              ，期间该节点会短暂断线。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => pending && run(pending)}>执行</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

// ---------------------------------------------------------------------------
// MachineCard（§7.2 契约 1）
// ---------------------------------------------------------------------------

export interface MachineCardProps {
  machine: Machine;
  /** Beszel 对应 system；agent 未就绪时不传，指标格退化为 — */
  system?: MonitorSystem;
  /** 整卡可点（H 的 B2 用它跳 /devices/<id>）；不传则卡片不可交互 */
  onClick?: () => void;
  /** E3 动作成功后让调用方刷新数据。契约 1 之外的可选扩展，不影响 H 的调用方式 */
  onActionDone?: () => void;
}

/**
 * 家宽公网行：配了 public_probe 才有；还没有任何结论（无样本、无说明）时整行不渲染，
 * 与色带行同一条「没数据不摆一行 —」规则。悬停看最近一次的探测说明。
 */
function PublicProbeRow({ machine }: { machine: Machine }) {
  const probe = machine.public_probe;
  const samples = machine.public_probe_history || [];
  if (!probe || (samples.length === 0 && !machine.public_probe_msg)) return null;
  const status = machine.public_probe_status;

  return (
    <div
      className="flex flex-col gap-1 rounded-md bg-muted/40 p-2"
      title={machine.public_probe_msg || undefined}
    >
      <div className="flex items-baseline justify-between text-[11px] leading-none">
        <span className="text-muted-foreground">
          家宽公网 · {probe.type} {probe.port}
        </span>
        <span
          className={cn(
            'font-mono tabular-nums',
            status === 'down' && 'text-signal-5',
            status !== 'up' && status !== 'down' && 'text-muted-foreground'
          )}
        >
          {publicProbeText(status, samples)}
        </span>
      </div>
      <ProbeBand samples={samples} />
    </div>
  );
}

/**
 * 统一机器卡（§4 布局规格，参照 Komari Glassmorphism 节点卡）。
 *
 * 六区：header / chips / 2×2 指标网格 / 三列细节行 / 色带行 / 底部。
 * **两类 role 共用同一骨架**——`machine.role` 只影响状态点取哪个字段
 * （server=`system.status`，node=`probe_status`），格子构成一致，
 * 调用方不再按 role 选组件（契约 1）。
 *
 * 刻意不做的事：
 * - 不显示 `container_count`：Beszel `info.ct` 不可信（NAS 报 2 实际 20+，§3.1）
 * - 不为 `temperature` 留格子：只有 nas 有，其余五台恒空（§3.1）
 * - 失败率那格不叫「丢包」：单次 TCP connect 测不出丢包（§4.3 末段）
 */
export function MachineCard({ machine, system, onClick, onActionDone }: MachineCardProps) {
  const status = machine.role === 'node' ? machine.probe_status : system?.status;
  const traffic = machine.traffic;
  const actions = machine.actions || [];
  const uptime = uptimeText(system?.uptime_sec);
  const price = priceText(machine.renewal);
  const value = remainingValue(machine.renewal);
  const load = system?.load_avg;
  const hasQuota = traffic != null && traffic.quota_gb > 0;
  // M1：预测本身只 hover 可见；只有「照这个速度会超」才值得占一个 chip（设计 §5.7 预测只作 warning）
  const projection = projectionText(traffic);
  const overProjected = traffic?.projection?.over_quota === true;
  // 家宽公网：机器在线（Tailscale 探活 up）不代表代理端口从家宽连得上，两者分开看
  const publicDown = machine.public_probe != null && machine.public_probe_status === 'down';
  // 底部显示业务地址（tailscale IP 刻意不经 /api/machines 外泄，probe_msg hover 里可见真实探测目标）
  const bandTarget = machine.public_host || system?.host;
  const clickable = onClick != null;

  // 四格的副行先算出来：subSlot 要按「行」判定，得先知道同行邻居有没有数据。
  const cpuSub = load && load.length >= 3 ? load.map((n) => n.toFixed(2)).join(', ') : undefined;
  const memSub = formatMbPair(system?.memory_used_mb, system?.memory_total_mb);
  const diskSub =
    system?.disk_used_gb != null && system?.disk_total_gb != null
      ? `${formatGb(system.disk_used_gb)} / ${formatGb(system.disk_total_gb)}`
      : undefined;
  const trafficSub = traffic
    ? hasQuota
      ? `${formatGb(traffic.used_gb)} / ${formatGb(traffic.quota_gb)}`
      : `本月累计 · ${traffic.month}`
    : undefined;
  // 一行两格：任一格有副行就两格都留位（对齐优先），整行都没有就整行不留（§4.3）。
  const row1Sub = cpuSub != null || memSub != null;
  const row2Sub = diskSub != null || trafficSub != null;

  return (
    <Card
      className={cn(clickable && 'cursor-pointer transition-colors hover:bg-card/90')}
      onClick={onClick}
      // 可点卡必须键盘可达；不可点时不加 role/tabIndex，免得给读屏器假交互暗示
      {...(clickable
        ? {
            role: 'button',
            tabIndex: 0,
            onKeyDown: (e: React.KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick?.();
              }
            },
          }
        : {})}
    >
      {/* ---- header ---- */}
      <CardHeader className="flex-row items-center justify-between space-y-0 p-3 pb-1.5">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={cn('h-2.5 w-2.5 shrink-0 rounded-full', statusTone(status))}
            title={status || 'unknown'}
          />
          <CardTitle className="truncate text-sm">{machine.label}</CardTitle>
        </div>
        {(machine.os || machine.region) && (
          <div className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
            {machine.os && (
              <span className="inline-flex items-center gap-1">
                <MonitorSmartphone className="h-3.5 w-3.5" />
                <span className="truncate">{machine.os}</span>
              </span>
            )}
            {/* emoji 旗：不引图标包（契约 3）。读屏器读不出国名，补 title */}
            {machine.region && <span title={machine.region}>{machine.region}</span>}
          </div>
        )}
      </CardHeader>

      <CardContent className="space-y-2 p-3 pt-0">
        {/* ---- chips ---- */}
        {(uptime || price || overProjected || publicDown) && (
          <div className="flex flex-wrap items-center gap-1.5">
            {uptime && (
              <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] tabular-nums text-muted-foreground">
                {uptime}
              </span>
            )}
            {price && (
              <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] tabular-nums text-muted-foreground">
                {price}
              </span>
            )}
            {/* M1：预测超配额。**恒为 warn 色**，不随超出幅度升级成 error——
                真正超了由 pct 色带和今日关注的实测那条报 */}
            {overProjected && (
              <span
                className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-700"
                title={projection || undefined}
              >
                预计超配额
              </span>
            )}
            {publicDown && (
              <span
                className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-700"
                title={machine.public_probe_msg || undefined}
              >
                家宽公网不通
              </span>
            )}
          </div>
        )}

        {/* ---- 2×2 指标网格 ---- */}
        <div className="grid grid-cols-2 gap-x-3 gap-y-2">
          <MetricCell
            icon={Cpu}
            label="CPU"
            pct={system?.cpu ?? null}
            sub={cpuSub}
            subSlot={row1Sub}
            // 温度只有 nas 有，其余五台恒空（§3.1）→ 降级成 hover，不为它留一格
            hint={system?.temperature != null ? `温度 ${system.temperature.toFixed(0)} °C` : undefined}
          />
          <MetricCell
            icon={MemoryStick}
            label="内存"
            pct={system?.memory_pct ?? null}
            sub={memSub}
            subSlot={row1Sub}
            hint={
              system?.memory_basis === 'available'
                ? `按可用内存（MemAvailable），不含 page cache${
                    system.memory_pct_raw != null
                      ? ` · Beszel 原值 ${system.memory_pct_raw.toFixed(1)}%`
                      : ''
                  }`
                : undefined
            }
          />
          <MetricCell
            icon={HardDrive}
            label="硬盘"
            pct={system?.disk_pct ?? null}
            sub={diskSub}
            subSlot={row2Sub}
          />
          {/* 无配额（nas）只显本月累计、不画条不告警——契约 4 */}
          <MetricCell
            icon={ArrowUpDown}
            label="流量"
            pct={hasQuota ? traffic!.pct ?? null : null}
            valueText={hasQuota ? undefined : traffic ? formatGb(traffic.used_gb) : '—'}
            sub={trafficSub}
            subSlot={row2Sub}
            hint={
              traffic
                ? `计数自 ${traffic.since || '未知'} 起算${
                    traffic.resets ? `，其间归零 ${traffic.resets} 次` : ''
                  }${hasQuota ? '' : '；未配配额，不计百分比也不告警'}${
                    projection ? `\n${projection}` : ''
                  }`
                : undefined
            }
          />
        </div>

        {/* ---- 三列细节行 ---- */}
        <div className="grid grid-cols-3 gap-1.5">
          <DetailColumn
            hint={system?.net_nic ? `计费网卡 ${system.net_nic}（不含 tailscale0）` : undefined}
            lines={[
              { icon: ChevronUp, text: formatRate(system?.net_up_bytes_sec) },
              { icon: ChevronDown, text: formatRate(system?.net_down_bytes_sec) },
            ]}
          />
          <DetailColumn
            hint={traffic ? `${traffic.month} 累计（计费网卡 ${traffic.nic || '自动'}）` : undefined}
            lines={[
              { icon: ArrowUp, text: traffic ? formatGb(traffic.sent_gb) : '—' },
              { icon: ArrowDown, text: traffic ? formatGb(traffic.recv_gb) : '—' },
            ]}
          />
          <DetailColumn
            hint={machine.renewal?.expires_at ? `到期 ${machine.renewal.expires_at}` : undefined}
            lines={[
              {
                icon: CalendarDays,
                text: machine.renewal && !isActiveRenewal(machine.renewal)
                  ? RENEWAL_STATUS_LABELS[machine.renewal.status!]
                  : machine.renewal?.days_left != null ? `剩余 ${machine.renewal.days_left} 天` : '长期',
              },
              {
                icon: Coins,
                text:
                  value != null
                    ? moneyText(value.toFixed(2), machine.renewal?.currency)
                    : machine.renewal?.cost === 0
                      ? '免费'
                      : '—',
              },
            ]}
          />
        </div>

        {/* ---- 色带行：无探测记录整行不渲染（§4.3），不摆一行「—」 ---- */}
        {machine.probe_history != null && machine.probe_history.length > 0 && (
          <ProbeBandRow samples={machine.probe_history} />
        )}
        <PublicProbeRow machine={machine} />

        {/* ---- 底部 ---- */}
        {(bandTarget || actions.length > 0) && (
          <div className="flex items-center justify-between gap-2 border-t pt-1.5 text-[11px] text-muted-foreground">
            <span className="min-w-0 truncate font-mono" title={machine.probe_msg}>
              {bandTarget || '—'}
              {machine.probe && ` · ${machine.probe.type} ${machine.probe.port}`}
            </span>
            {actions.length > 0 && (
              <NodeActions machine={machine} actions={actions} onDone={onActionDone} />
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
