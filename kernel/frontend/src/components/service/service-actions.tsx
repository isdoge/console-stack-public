import { ArrowUpCircle, MoreVertical, PlayCircle, RotateCcw, ScrollText, Settings, StopCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { Service, ServiceAction } from '@/lib/types';

interface Props {
  svc: Service;
  disabled?: boolean;
  onSettings: (svc: Service) => void;
  onAction: (svc: Service, action: ServiceAction) => void;
}

/** 与后端动作白名单一致；未登记管理单元的进程没有容器动作。 */
export function allowedActions(svc: Service): Set<ServiceAction> {
  if (svc.type === 'process' && !svc.systemd_unit) return new Set();
  if (svc.actions === undefined || svc.actions === null) {
    return new Set<ServiceAction>(svc.type === 'process' ? [] : ['start', 'stop', 'restart', 'logs']);
  }
  return new Set(svc.actions);
}

export function ServiceActions({ svc, disabled, onSettings, onAction }: Props) {
  const running = svc.running === 'running';
  const stopped = svc.running === 'stopped';
  const allowed = allowedActions(svc);
  const can = (action: ServiceAction) => allowed.has(action);
  const showLogs = can('logs');
  const showStart = can('start') && (stopped || !running);
  const showStop = can('stop') && (running || !stopped);
  const showRestart = can('restart');
  // upgrade 仅当台账 actions 显式含 upgrade（后端要求 compose_dir，默认动作集不含它）
  const showUpgrade = can('upgrade');
  const hasServiceActions = showStart || showStop || showRestart || showLogs || showUpgrade;

  return (
    <div className="inline-flex items-center">
      {showLogs ? (
        <Button
          size="icon" className="h-[30px] w-[30px] md:h-9 md:w-9"
          variant="ghost"
          disabled={disabled}
          aria-label="查看日志"
          title="查看日志"
          onClick={() => onAction(svc, 'logs')}
        >
          <ScrollText className="h-4 w-4" />
        </Button>
      ) : null}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="icon" className="h-[30px] w-[30px] md:h-9 md:w-9" variant="ghost" disabled={disabled} aria-label="操作">
            <MoreVertical className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-40">
          <DropdownMenuItem className="min-h-[30px] md:min-h-0" onClick={() => onSettings(svc)}>
            <Settings className="mr-2 h-4 w-4" />
            设置
          </DropdownMenuItem>
          {hasServiceActions ? <DropdownMenuSeparator /> : null}
          {showStart ? (
            <DropdownMenuItem className="min-h-[30px] md:min-h-0" onClick={() => onAction(svc, 'start')}>
              <PlayCircle className="mr-2 h-4 w-4 text-emerald-500" />
              启动
            </DropdownMenuItem>
          ) : null}
          {showStop ? (
            <DropdownMenuItem className="min-h-[30px] md:min-h-0" onClick={() => onAction(svc, 'stop')}>
              <StopCircle className="mr-2 h-4 w-4 text-red-500" />
              停止
            </DropdownMenuItem>
          ) : null}
          {showRestart ? (
            <DropdownMenuItem className="min-h-[30px] md:min-h-0" onClick={() => onAction(svc, 'restart')}>
              <RotateCcw className="mr-2 h-4 w-4 text-amber-500" />
              重启
            </DropdownMenuItem>
          ) : null}
          {showLogs ? (
            <DropdownMenuItem className="min-h-[30px] md:min-h-0" onClick={() => onAction(svc, 'logs')}>
              <ScrollText className="mr-2 h-4 w-4" />
              日志
            </DropdownMenuItem>
          ) : null}
          {showUpgrade ? (
            <DropdownMenuItem className="min-h-[30px] md:min-h-0" onClick={() => onAction(svc, 'upgrade')}>
              <ArrowUpCircle className="mr-2 h-4 w-4 text-sky-500" />
              升级
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
