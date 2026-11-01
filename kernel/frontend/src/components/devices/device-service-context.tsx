import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowUpCircle,
  Boxes,
  ExternalLink,
  FileCode2,
  GitBranch,
  Layers,
  Network,
  PlayCircle,
  RotateCcw,
  ScrollText,
  Settings,
  ShieldCheck,
  StopCircle,
} from 'lucide-react';
import { toast } from 'sonner';
import { NavigationLink } from '@/components/navigation-link';
import { serviceNavigation, type NavigationContribution } from '@/lib/plugins/navigation-types';
import {
   certDisplay,
  HealthMiniBand,
  identityKey,
  isOutdated,
  isStatusUp,
  UnifiedTable,
} from '@/components/dashboard/unified-table';
import { AccessBadges } from '@/components/service/access-badges';
import { EntryChain } from '@/components/service/entry-chain';
import { LogsDialog } from '@/components/service/logs-dialog';
import { ReverseProxyRoutes } from '@/components/service/reverse-proxy-routes';
import { allowedActions } from '@/components/service/service-actions';
import { isKnownVersion } from '@/components/service/version-cell';
import { VersionDialog } from '@/components/service/version-dialog';
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
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { DisplayTime } from '@/components/ui/display-time';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { apiErrorMessage, runServiceAction } from '@/lib/api';
import {
  safeWebLink,
  serviceProject,
} from '@/lib/service-context';
import type {
  HealthProbeSpec,
  Machine,
  MonitorContainer,
  Service,
  ServiceAction,
} from '@/lib/types';
import { cn, copyText } from '@/lib/utils';
import { displayVersion } from '@/lib/version-display';

// ---------------------------------------------------------------------------
// B1-1 §2.2 / §4.2 / §5.4 契约类型与纯函数（导出供 B3-1/B3-4 与单测复用）
// ---------------------------------------------------------------------------

/**
 * B1-1 §4.2 ContainerSnapshot 前端消费扩展（向下兼容现有 MonitorContainer）。
 */
export interface ContainerContextSnapshot extends MonitorContainer {
  /** B1-1 §2.2：跨源稳定机器主键（小写：nas/txy/aly/kr/sg/us）。 */
  machine_id?: string;
  /** B1-1 §2.2：Docker label `com.docker.compose.project`；缺失或 default 归「未分组」。 */
  compose_project?: string | null;
  /** B1-1 §4.2：容器行最后更新时间（ISO 8601 UTC）。 */
  updated_at?: string | null;
  ports?: string;
}

export interface ControlledActionItem {
  action: ServiceAction;
  label: string;
  /** stop / restart / upgrade 需二次确认，防止误触并支持生产环境无损验证按钮存在 */
  requiresConfirm: boolean;
  /** 当前运行态下是否推荐/可点（例如已 running 时 start 置灰，避免无效重复操作） */
  enabled: boolean;
  reason?: string;
}

const ACTION_META: Record<
  ServiceAction,
  { label: string; requiresConfirm: boolean }
> = {
  start: { label: '启动', requiresConfirm: false },
  stop: { label: '停止', requiresConfirm: true },
  restart: { label: '重启', requiresConfirm: true },
  logs: { label: '查看日志', requiresConfirm: false },
  upgrade: { label: '升级', requiresConfirm: true },
};

const ACTION_ORDER: ServiceAction[] = [
  'logs',
  'restart',
  'start',
  'stop',
  'upgrade',
];

const PRIORITY_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
};

/**
 * B1-1 §2.2：按跨源稳定键（`machine_id` + 容器资源名）匹配登记服务对应的容器实时快照。
 * 绝不跨机器合并同名容器。
 */
export function matchServiceContainer(
  service?: Service | null,
  containers?: ContainerContextSnapshot[] | null
): ContainerContextSnapshot | null {
  if (!service || !containers || containers.length === 0) return null;
  const targetKey = identityKey(service.machine, service.container || service.name);
  for (const c of containers) {
    const cMachine = c.machine_id || c.machine || c.system_name;
    if (identityKey(cMachine, c.name) === targetKey) {
      return c;
    }
  }
  return null;
}

/**
 * B1-1 §2.2 规则 3：Compose 栈分组取自官方 label，缺失或兜底值 `default` 时归「未分组」，不凭容器名猜测。
 */
export function resolveComposeGroup(
  container?: ContainerContextSnapshot | null
): string {
  const raw = container?.compose_project?.trim();
  if (!raw || raw.toLowerCase() === 'default') return '未分组';
  return raw;
}

/**
 * 复用 `allowedActions(service)` 白名单，生成服务上下文的受控动作描述列表。
 * - 严格遵守台账白名单与进程/systemd 约束。
 * - `upgrade` 额外核对机器级升级闸（若传入 `machine` 且 `machine.upgrade?.enabled === false` 则禁用并说明原因）。
 */
export function resolveServiceControlledActions(
  service?: Service | null,
  options?: {
    machine?: Machine | null;
    runStatus?: string;
  }
): ControlledActionItem[] {
  if (!service) return [];
  const allowed = allowedActions(service);
  const rawStatus = options?.runStatus ?? service.running;
  const running = rawStatus === 'running' || isStatusUp(rawStatus);
  const stopped =
    rawStatus === 'stopped' ||
    (typeof rawStatus === 'string' &&
      rawStatus.trim() !== '' &&
      !isStatusUp(rawStatus));

  const out: ControlledActionItem[] = [];
  for (const action of ACTION_ORDER) {
    if (!allowed.has(action)) continue;
    const meta = ACTION_META[action];

    if (action === 'start') {
      out.push({
        action,
        label: meta.label,
        requiresConfirm: meta.requiresConfirm,
        enabled: stopped || !running,
        reason: running ? '服务当前已在运行中' : undefined,
      });
      continue;
    }

    if (action === 'stop') {
      out.push({
        action,
        label: meta.label,
        requiresConfirm: meta.requiresConfirm,
        enabled: running || !stopped,
        reason: stopped ? '服务当前已停止' : undefined,
      });
      continue;
    }

    if (action === 'upgrade') {
      const machineGateClosed =
        options?.machine != null && options.machine.upgrade?.enabled !== true;
      const missingComposeDir = !service.compose_dir;
      const enabled = !machineGateClosed && !missingComposeDir;
      out.push({
        action,
        label: meta.label,
        requiresConfirm: meta.requiresConfirm,
        enabled,
        reason: missingComposeDir
          ? '未配置 compose_dir'
          : machineGateClosed
            ? '所在设备未开启升级闸（upgrade.enabled）'
            : undefined,
      });
      continue;
    }

    out.push({
      action,
      label: meta.label,
      requiresConfirm: meta.requiresConfirm,
      enabled: true,
    });
  }
  return out;
}

/**
 * L1 健康探测配置摘要文案（只读展示，不暴露任何凭据）。
 */
export function formatHealthProbeConfig(
  probe?: boolean | HealthProbeSpec
): string {
  if (probe === false) return '已关闭（不探测）';
  if (!probe || probe === true) return '默认推断（按端口/类型探活）';
  const parts: string[] = [`HTTP 语义检查 · 目标 ${probe.target}`];
  if (probe.expected_status === null) {
    parts.push('不校验状态码');
  } else {
    parts.push(`状态码 ${probe.expected_status ?? 200}`);
  }
  if (probe.max_latency_ms != null) {
    parts.push(`阈值 ≤${probe.max_latency_ms}ms`);
  }
  if (probe.body_contains) {
    parts.push('含响应体特征匹配');
  }
  return parts.join(' · ');
}

// ---------------------------------------------------------------------------
// DeviceServiceContext 主组件（兼容现有 ServiceContext，增强四维上下文与受控动作）
// ---------------------------------------------------------------------------

export interface DeviceServiceContextProps {
  /** 当前选中的服务；为 null 时抽屉关闭（inline 模式下渲染空态提示） */
  service: Service | null;
  /** 插件提供的导航投影，只含入口与同设备精确匹配信息。 */
  navigation?: NavigationContribution[];
  /** 可选：容器快照列表或单个已匹配容器，用于补齐实时资源与 compose_project 分组 */
  containers?: ContainerContextSnapshot[];
  container?: ContainerContextSnapshot | null;
  /** 可选：所属设备台账对象，用于核对 upgrade 闸门与显示设备标签 */
  machine?: Machine | null;
  /** 外部动作执行中禁用态 */
  disabled?: boolean;
  /** 渲染形态：默认 'sheet' 侧边抽屉（与现有 ServiceContext 完全兼容）；'inline' 供嵌页或单测直渲 */
  mode?: 'sheet' | 'inline';
  /** 可选：最近一次读取到的日志文本（传入时在上下文内提供紧凑预览） */
  recentLogs?: string;
  onClose: () => void;
  /** 兼容旧调用方：点击查看日志回调 */
  onLogs?: (service: Service) => void;
  /** B3 受控动作统一回调；未传时组件内部自动调用 runServiceAction 闭环 */
  onAction?: (service: Service, action: ServiceAction) => void | Promise<void>;
  /** 打开服务配置编辑抽屉 */
  onSettings?: (service: Service) => void;
  /** 打开版本对比与升级准备弹窗 */
  onVersion?: (service: Service) => void;
  /** 内部直接执行动作成功后的刷新回调 */
  onActionDone?: () => void;
}

export function DeviceServiceContext({
  service,
  navigation = [],
  containers,
  container: explicitContainer,
  machine,
  disabled = false,
  mode = 'sheet',
  recentLogs,
  onClose,
  onLogs,
  onAction,
  onSettings,
  onVersion,
  onActionDone,
}: DeviceServiceContextProps) {

  const [pendingAction, setPendingAction] = useState<ControlledActionItem | null>(
    null
  );
  const [internalRunning, setInternalRunning] = useState(false);
  const [internalLogs, setInternalLogs] = useState<string | null>(null);
  const [internalLogsOpen, setInternalLogsOpen] = useState(false);

  const matchedContainer = useMemo(
    () => explicitContainer ?? matchServiceContainer(service, containers),
    [explicitContainer, service, containers]
  );

  const runStatus = matchedContainer?.status || service?.running;
  const isUp = isStatusUp(runStatus);
  const controlledActions = useMemo(
    () =>
      resolveServiceControlledActions(service, {
        machine,
        runStatus,
      }),
    [service, machine, runStatus]
  );

  const busy = disabled || internalRunning;

  const executeAction = async (action: ServiceAction) => {
    if (!service) return;
    if (action === 'logs' && onLogs && !onAction) {
      onClose();
      onLogs(service);
      return;
    }
    if (onAction) {
      if (action === 'logs') onClose();
      await onAction(service, action);
      return;
    }

    // 自包含兜底：调用方未传 onAction 时，直接走后端白名单接口
    const meta = ACTION_META[action];
    setInternalRunning(true);
    try {
      const res = await runServiceAction(service.name, action);
      if (action === 'logs') {
        const text = res.output || '无日志';
        setInternalLogs(text);
        setInternalLogsOpen(true);
      } else if (res.ok) {
        if (res.warning) toast.warning(res.warning);
        else toast.success(`${service.name}：${meta.label}成功`);
        onActionDone?.();
      } else {
        toast.error(`${meta.label}失败: ${res.output || res.error || ''}`);
      }
    } catch (err: unknown) {
      toast.error(`操作失败: ${apiErrorMessage(err)}`);
    } finally {
      setInternalRunning(false);
    }
  };

  const handleActionTrigger = (item: ControlledActionItem) => {
    if (!item.enabled || busy) return;
    if (item.requiresConfirm) {
      setPendingAction(item);
      return;
    }
    void executeAction(item.action);
  };

  const relatedNavigation = service ? serviceNavigation(navigation, service) : [];
  const project = service ? serviceProject(service) : null;
  const repo = safeWebLink(project?.repo || service?.version?.repo_url);
  const cert = service ? certDisplay(service.health) : null;
  const outdated = service ? isOutdated(service) : false;
  const localVersion = service?.version_info?.local || 'N/A';
  const upstreamVersion = service?.version_info?.upstream || 'N/A';
  const hasVersionDetail = Boolean(
    service?.version?.enabled ||
      service?.version?.repo_url ||
      isKnownVersion(service?.version_info?.upstream)
  );
  const composeGroup = resolveComposeGroup(matchedContainer);
  const logsPreview = recentLogs ?? internalLogs;

  const actionIcon = (action: ServiceAction) => {
    switch (action) {
      case 'start':
        return <PlayCircle className="mr-1.5 h-3.5 w-3.5 text-emerald-500" />;
      case 'stop':
        return <StopCircle className="mr-1.5 h-3.5 w-3.5 text-red-500" />;
      case 'restart':
        return <RotateCcw className="mr-1.5 h-3.5 w-3.5 text-amber-500" />;
      case 'logs':
        return <ScrollText className="mr-1.5 h-3.5 w-3.5" />;
      case 'upgrade':
        return <ArrowUpCircle className="mr-1.5 h-3.5 w-3.5 text-sky-500" />;
    }
  };

  const body = service ? (
    <div
      className="space-y-5"
      data-service-context={ `${service.machine}:${service.name}` }
    >
      {/* 1. 服务详情：运行/健康状态与版本/更新提示严格分区（B1-1 §5.4 & R7） */}
      <section className="space-y-2.5" data-context-section="details">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <ShieldCheck className="h-4 w-4 text-muted-foreground" />
            <span>服务详情与健康</span>
          </h3>
          <span className="font-mono text-xs text-muted-foreground">
            {identityKey(service.machine, service.container || service.name)}
          </span>
        </div>

        {/* 运行与探活健康卡 */}
        <div className="rounded-md border bg-muted/25 p-3 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant={isUp ? 'success' : 'secondary'}>
                {isUp ? 'running' : runStatus || 'unknown'}
              </Badge>
              {service.health?.status && (
                <Badge
                  variant={
                    service.health.status === 'Healthy'
                      ? 'success'
                      : service.health.status === 'Unhealthy'
                        ? 'destructive'
                        : 'outline'
                  }
                >
                  探活: {service.health.status}
                </Badge>
              )}
              {cert && (
                <span
                  className={cn(
                    'rounded-full border px-2 py-0.5 font-mono text-[11px]',
                    cert.tone === 'bad'
                      ? 'border-destructive/40 bg-destructive/10 text-destructive'
                      : cert.tone === 'warn'
                        ? 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300'
                        : 'border-border text-muted-foreground'
                  )}
                >
                  SSL {cert.text}
                </span>
              )}
            </div>
            <HealthMiniBand samples={service.health_history} />
          </div>

          {service.health?.msg && (
            <p className="text-xs text-muted-foreground break-words">
              探活说明：{service.health.msg}
            </p>
          )}

          {/* 容器实时资源读数（匹配到 MonitorContainer 时展示） */}
          {matchedContainer && (
            <div className="grid grid-cols-3 gap-2 pt-1 border-t border-border/60 text-xs">
              <div>
                <span className="text-muted-foreground">CPU：</span>
                <span className="font-mono tabular-nums">
                  {matchedContainer.cpu != null ? `${matchedContainer.cpu}%` : '—'}
                </span>
              </div>
              <div>
                <span className="text-muted-foreground">内存：</span>
                <span className="font-mono tabular-nums">
                  {matchedContainer.memory_mb != null
                    ? `${matchedContainer.memory_mb} MiB`
                    : '—'}
                </span>
              </div>
              <div>
                <span className="text-muted-foreground">网络 IO：</span>
                <span className="font-mono tabular-nums">
                  {matchedContainer.net_io ?? '—'}
                </span>
              </div>
            </div>
          )}
          {matchedContainer?.updated_at && (
            <div className="text-[11px] text-muted-foreground">
              容器采样时间：<DisplayTime value={matchedContainer.updated_at} />
            </div>
          )}
        </div>

        {/* 独立版本与更新提示区（B1-1 §5.4：更新提示非故障，采用中性天蓝提示色） */}
        <div
          className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-card px-3 py-2 text-xs"
          data-version-health-separated="true"
        >
          <div className="flex flex-wrap items-center gap-2">
            <GitBranch className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-muted-foreground">当前版本</span>
            <span className="font-mono font-medium">{displayVersion(localVersion)}</span>
            {isKnownVersion(upstreamVersion) && (
              <span className="text-muted-foreground font-mono">
                （上游 {displayVersion(upstreamVersion)}）
              </span>
            )}
            {outdated && (
              <span
                className="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[11px] font-medium text-sky-700 dark:text-sky-300"
                data-service-update-badge
              >
                可更新（非故障）
              </span>
            )}
          </div>
          {hasVersionDetail && onVersion && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => onVersion(service)}
            >
              版本详情
            </Button>
          )}
        </div>
      </section>

      {/* 2. 受控动作与日志记录（白名单动作 + 二次确认防呆 + 日志与审计入口） */}
      <section className="space-y-2.5" data-context-section="actions-logs">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <ScrollText className="h-4 w-4 text-muted-foreground" />
          <span>受控动作与运行记录</span>
        </h3>

        {controlledActions.length > 0 ? (
          <div className="flex flex-wrap gap-2" data-controlled-actions>
            {controlledActions.map((item) => (
              <Button
                key={item.action}
                variant="outline"
                size="sm"
                disabled={busy || !item.enabled}
                title={item.reason}
                data-action-key={item.action}
                onClick={() => handleActionTrigger(item)}
              >
                {actionIcon(item.action)}
                {item.label}
              </Button>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            此服务未开放容器或 systemd 受控动作（白名单为空）。
          </p>
        )}

        {!allowedActions(service).has('logs') && (
          <p className="text-xs text-muted-foreground">此服务未开放日志读取。</p>
        )}

        {/* 内联最近日志摘要（若有） */}
        {logsPreview && (
          <div className="space-y-1.5 rounded-md border bg-muted/30 p-2.5">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>最近日志输出</span>
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs"
                  onClick={() =>
                    copyText(logsPreview)
                      .then(() => toast.success('日志已复制'))
                      .catch(() => toast.error('复制失败'))
                  }
                >
                  复制
                </Button>
                {internalLogs && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    onClick={() => setInternalLogsOpen(true)}
                  >
                    全屏查看
                  </Button>
                )}
              </div>
            </div>
            <pre className="max-h-36 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed">
              {logsPreview}
            </pre>
          </div>
        )}

        <div className="flex flex-wrap gap-2 pt-1">
          <Button variant="outline" size="sm" asChild>
            <Link
              to={`/devices/${encodeURIComponent(service.machine)}`}
              onClick={onClose}
            >
              所在设备
            </Link>
          </Button>
        </div>
      </section>

      {/* 3. 服务配置摘要（类型、端口、探活规则、Compose 路径、设置入口） */}
      <section className="space-y-2.5" data-context-section="config">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <FileCode2 className="h-4 w-4 text-muted-foreground" />
            <span>配置摘要</span>
          </h3>
          {onSettings && (
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-2.5 text-xs"
              disabled={busy}
              onClick={() => {
                onClose();
                onSettings(service);
              }}
            >
              <Settings className="mr-1 h-3.5 w-3.5" />
              编辑配置
            </Button>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="rounded-md bg-muted/40 px-2.5 py-2">
            <div className="text-muted-foreground">运行形态 / 单元</div>
            <div className="mt-0.5 truncate font-mono">
              {service.type === 'process'
                ? `process${service.systemd_unit ? ` · ${service.systemd_unit}` : ''}${
                    service.systemd_scope ? ` (${service.systemd_scope})` : ''
                  }`
                : `docker · ${service.container || service.name}`}
            </div>
          </div>
          <div className="rounded-md bg-muted/40 px-2.5 py-2">
            <div className="text-muted-foreground">端口 / 优先级</div>
            <div className="mt-0.5 truncate font-mono">
              {service.port ?? '未登记端口'} · 优先级{' '}
              {PRIORITY_LABELS[service.priority || 'medium'] ||
                service.priority ||
                '中'}
            </div>
          </div>
          <div className="col-span-2 rounded-md bg-muted/40 px-2.5 py-2">
            <div className="text-muted-foreground">健康探测配置</div>
            <div className="mt-0.5 font-mono text-[11px]">
              {formatHealthProbeConfig(service.health_probe)}
            </div>
          </div>
          {(service.compose_dir || service.deploy || matchedContainer?.image) && (
            <div className="col-span-2 rounded-md bg-muted/40 px-2.5 py-2 space-y-1">
              {matchedContainer?.image && (
                <div className="truncate font-mono text-[11px]" title={matchedContainer.image}>
                  <span className="text-muted-foreground">镜像：</span>
                  {matchedContainer.image}
                </div>
              )}
              {service.compose_dir && (
                <div className="truncate font-mono text-[11px]" title={service.compose_dir}>
                  <span className="text-muted-foreground">Compose：</span>
                  {service.compose_dir}
                  {service.compose_file ? ` / ${service.compose_file}` : ''}
                </div>
              )}
              {service.deploy && (
                <div className="truncate text-[11px]" title={service.deploy}>
                  <span className="text-muted-foreground">部署方式：</span>
                  {service.deploy}
                </div>
              )}
            </div>
          )}
          {(service.note || (service.tags && service.tags.length > 0)) && (
            <div className="col-span-2 rounded-md bg-muted/40 px-2.5 py-2 space-y-1">
              {service.tags && service.tags.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {service.tags.map((tag) => (
                    <Badge key={tag} variant="outline" className="text-[10px]">
                      {tag}
                    </Badge>
                  ))}
                </div>
              )}
              {service.note && (
                <div className="text-muted-foreground break-words">{service.note}</div>
              )}
            </div>
          )}
        </div>
      </section>

      {/* 4. 依赖关系与入口拓扑（入口链路、探活可达性、反代路由、Compose 栈、关联面板/仓库/成本） */}
      <section className="space-y-2.5" data-context-section="dependencies">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Network className="h-4 w-4 text-muted-foreground" />
          <span>依赖关系与入口链路</span>
        </h3>

        <div className="rounded-md border bg-muted/20 p-3 space-y-2.5 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground shrink-0">入口拓扑</span>
            <EntryChain entry={service.entry} />
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-border/50 pt-2">
            <span className="text-muted-foreground shrink-0">
              访问探活（H｜T｜I）
            </span>
            <AccessBadges svc={service} />
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-border/50 pt-2">
            <span className="inline-flex items-center gap-1 text-muted-foreground shrink-0">
              <Layers className="h-3.5 w-3.5" />
              Compose 栈分组
            </span>
            <span className="font-mono">{composeGroup}</span>
          </div>
          {(service.proxy?.provider ||
            service.proxy?.target?.machine ||
            service.proxy?.target?.container ||
            (service.proxy_routes && service.proxy_routes.length > 0) ||
            service.proxy_error) && (
            <div className="border-t border-border/50 pt-2 space-y-1.5">
              <div className="flex items-center justify-between text-muted-foreground">
                <span>反向代理与上游依赖</span>
                {service.proxy?.provider && (
                  <span className="font-mono text-[11px]">
                    {service.proxy.provider}
                    {service.proxy.target?.machine
                      ? ` -> ${service.proxy.target.machine}`
                      : ''}
                    {service.proxy.target?.container
                      ? `/${service.proxy.target.container}`
                      : ''}
                  </span>
                )}
              </div>
              <ReverseProxyRoutes svc={service} />
            </div>
          )}
        </div>

        {relatedNavigation.map((group) => (
          <div key={group.owner} className="space-y-1.5">
            <div className="text-xs font-medium text-muted-foreground">{group.label}</div>
            {group.links.length > 0 ? (
              <div className="flex flex-wrap gap-2">{group.links.map((link) => (
                <Button key={link.id} variant="outline" size="sm" asChild>
                  <NavigationLink target={link} onClick={onClose}>{link.label}</NavigationLink>
                </Button>
              ))}</div>
            ) : <p className="text-xs text-muted-foreground">{group.emptyMessage}</p>}
          </div>
        ))}

        {/* 仓库与项目资料 */}
        <div className="space-y-1.5">
          <div className="text-xs font-medium text-muted-foreground">
            仓库与项目资料
          </div>
          <div className="flex flex-wrap gap-2">
            {repo && (
              <Button variant="outline" size="sm" asChild>
                <a href={repo} target="_blank" rel="noopener noreferrer">
                  {project ? '项目仓库' : '上游仓库'}
                  <ExternalLink className="ml-1 h-3 w-3" />
                </a>
              </Button>
            )}
            {project?.links.map((link) => (
              <Button key={link.href} variant="outline" size="sm" asChild>
                <a href={link.href} target="_blank" rel="noopener noreferrer">
                  {link.label}
                </a>
              </Button>
            ))}
          </div>
          {!project && (
            <p className="text-xs text-muted-foreground">
              {repo
                ? '本地项目资料未登记，可先查看上游仓库。'
                : '尚未登记仓库或项目资料。'}
            </p>
          )}
        </div>

      </section>

      {/* 受控动作二次确认弹窗（stop / restart / upgrade） */}
      <AlertDialog
        open={pendingAction != null}
        onOpenChange={(open) => !open && setPendingAction(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              确认在 {service.machine} 上对 {service.name} 执行「
              {pendingAction?.label}」？
            </AlertDialogTitle>
            <AlertDialogDescription>
              该动作属于服务白名单受控操作，执行结果将写入操作审计（oplog）。
              {pendingAction?.action === 'stop' ||
              pendingAction?.action === 'restart'
                ? '执行期间该服务会短暂中断响应。'
                : ''}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const target = pendingAction;
                setPendingAction(null);
                if (target) void executeAction(target.action);
              }}
            >
              确认{pendingAction?.label}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 自包含日志弹窗（当调用方未外部接管 onLogs 时使用） */}
      <LogsDialog
        open={internalLogsOpen}
        logs={internalLogs || ''}
        onOpenChange={setInternalLogsOpen}
      />
    </div>
  ) : (
    <div className="py-6 text-center text-sm text-muted-foreground">
      未选择服务，点击紧凑服务表中的服务名称即可展开上下文与受控动作。
    </div>
  );

  if (mode === 'inline') {
    return (
      <div className="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
        {service && (
          <div className="mb-4 flex items-start justify-between gap-2 border-b pb-3">
            <div>
              <div className="flex items-center gap-1.5 text-base font-semibold">
                <Boxes className="h-4 w-4 text-muted-foreground" />
                <span>{service.name} · 服务上下文</span>
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {service.machine} · {service.purpose || '服务详情、配置、依赖与受控动作'}
              </p>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose}>
              收起
            </Button>
          </div>
        )}
        {body}
      </div>
    );
  }

  return (
    <Sheet
      open={Boolean(service)}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>{service?.name} · 服务上下文</SheetTitle>
          <SheetDescription>
            {service?.machine} ·{' '}
            {service?.purpose || '服务详情、日志、配置、依赖关系与受控动作'}
          </SheetDescription>
        </SheetHeader>
        {service && <div className="mt-6">{body}</div>}
      </SheetContent>
    </Sheet>
  );
}

// ---------------------------------------------------------------------------
// DeviceServiceSection：保留紧凑服务表 UnifiedTable + 增强服务上下文联动容器
// ---------------------------------------------------------------------------

export interface DeviceServiceSectionProps {
  services: Service[];
  containers: ContainerContextSnapshot[];
  navigation?: NavigationContribution[];
  machine?: Machine | null;
  activeMachine?: string;
  loading?: boolean;
  subtitle?: React.ReactNode;
  actionDisabled?: boolean;
  statFilter?: 'running' | 'healthy' | 'outdated' | null;
  /** 可选受控上下文服务；不传则由组件内部 state 管理点击服务名展开 */
  contextService?: Service | null;
  onContextChange?: (svc: Service | null) => void;
  onAdd?: () => void;
  onSettings?: (svc: Service) => void;
  onAction?: (svc: Service, action: ServiceAction) => void | Promise<void>;
  onVersion?: (svc: Service) => void;
  onBatchPriority?: (names: string[], priority: string) => Promise<void>;
  onRefresh?: () => void;
}

/**
 * B3-3 设备页服务区组合组件：
 * - 全部视图与单机视图继续复用现有紧凑服务表 `UnifiedTable`（不替换成大卡墙）。
 * - 点击服务名即展开 `DeviceServiceContext` 抽屉，打通「紧凑服务表 → 服务上下文 → 受控动作」。
 */
export function DeviceServiceSection({
  services,
  containers,
  navigation = [],
  machine,
  activeMachine,
  loading = false,
  subtitle,
  actionDisabled = false,
  statFilter = null,
  contextService: controlledContext,
  onContextChange,
  onAdd = () => {},
  onSettings = () => {},
  onAction,
  onVersion,
  onBatchPriority = async () => {},
  onRefresh,
}: DeviceServiceSectionProps) {
  const [internalContext, setInternalContext] = useState<Service | null>(null);
  const [versionOpen, setVersionOpen] = useState(false);
  const [versionName, setVersionName] = useState('');
  const [logsOpen, setLogsOpen] = useState(false);
  const [logsText, setLogsText] = useState('');
  const [runningAction, setRunningAction] = useState(false);

  const selectedContext =
    controlledContext !== undefined ? controlledContext : internalContext;

  const setContext = (svc: Service | null) => {
    if (onContextChange) onContextChange(svc);
    else setInternalContext(svc);
  };

  // 保持上下文里的 service 随最新 services 列表同步更新
  const activeService = useMemo(() => {
    if (!selectedContext) return null;
    return (
      services.find(
        (s) =>
          s.name === selectedContext.name &&
          s.machine === selectedContext.machine
      ) ?? selectedContext
    );
  }, [services, selectedContext]);

  const handleVersion = (svc: Service) => {
    if (onVersion) {
      onVersion(svc);
      return;
    }
    setVersionName(svc.name);
    setVersionOpen(true);
  };

  const handleAction = async (svc: Service, action: ServiceAction) => {
    if (onAction) {
      await onAction(svc, action);
      return;
    }
    const label = ACTION_META[action].label;
    setRunningAction(true);
    try {
      const res = await runServiceAction(svc.name, action);
      if (action === 'logs') {
        setLogsText(res.output || '无日志');
        setLogsOpen(true);
      } else if (res.ok) {
        if (res.warning) toast.warning(res.warning);
        else toast.success(`${label}成功`);
        onRefresh?.();
      } else {
        toast.error(`${label}失败: ${res.output || res.error || ''}`);
      }
    } catch (err: unknown) {
      toast.error(`操作失败: ${apiErrorMessage(err)}`);
    } finally {
      setRunningAction(false);
    }
  };

  return (
    <div className="space-y-4" data-device-service-section>
      <UnifiedTable
        services={services}
        containers={containers}
        loading={loading}
        subtitle={subtitle}
        activeMachine={activeMachine ?? machine?.id}
        actionDisabled={actionDisabled || runningAction}
        statFilter={statFilter}
        onAdd={onAdd}
        onSettings={onSettings}
        onAction={(svc, action) => {
          void handleAction(svc, action);
        }}
        onVersion={handleVersion}
        onContext={(svc) => setContext(svc)}
        onBatchPriority={onBatchPriority}
      />

      <DeviceServiceContext
        service={activeService}
        containers={containers}
        navigation={navigation}
        machine={machine}
        disabled={actionDisabled || runningAction}
        recentLogs={logsText || undefined}
        onClose={() => setContext(null)}
        onAction={handleAction}
        onSettings={onSettings}
        onVersion={handleVersion}
        onActionDone={onRefresh}
      />

      {!onAction && (
        <LogsDialog
          open={logsOpen}
          logs={logsText}
          onOpenChange={setLogsOpen}
        />
      )}
      {!onVersion && (
        <VersionDialog
          open={versionOpen}
          name={versionName}
          onOpenChange={setVersionOpen}
        />
      )}
    </div>
  );
}

/** 别名导出，方便直接替换既有 ServiceContext 导入。 */
export { DeviceServiceContext as ServiceContext };
