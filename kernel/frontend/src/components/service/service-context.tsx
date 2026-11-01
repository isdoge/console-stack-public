import { NavigationLink } from '@/components/navigation-link';
import { serviceNavigation, type NavigationContribution } from '@/lib/plugins/navigation-types';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { allowedActions } from './service-actions';
import { safeWebLink, serviceProject } from '@/lib/service-context';
import type { Service } from '@/lib/types';

interface Props {
  service: Service | null;
  navigation?: NavigationContribution[];
  disabled?: boolean;
  onClose: () => void;
  onLogs: (service: Service) => void;
}

export function ServiceContext({ service, navigation = [], disabled, onClose, onLogs }: Props) {
  const related = service ? serviceNavigation(navigation, service) : [];
  const project = service ? serviceProject(service) : null;
  const repo = safeWebLink(project?.repo || service?.version?.repo_url);
  return (
    <Sheet open={Boolean(service)} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>{service?.name} · 服务上下文</SheetTitle>
          <SheetDescription>{service?.machine} · {service?.purpose || '服务资料与常用入口'}</SheetDescription>
        </SheetHeader>
        {service && <div className="mt-6 space-y-6">
          <section className="space-y-2">
            <h3 className="text-sm font-semibold">运行与记录</h3>
            <div className="flex flex-wrap gap-2">
              {allowedActions(service).has('logs') && <Button variant="outline" disabled={disabled}
                onClick={() => { onClose(); onLogs(service); }}>查看日志</Button>}
              <Button variant="outline" asChild><Link to={`/devices/${encodeURIComponent(service.machine)}`} onClick={onClose}>所在设备</Link></Button>
            </div>
            {!allowedActions(service).has('logs') && <p className="text-xs text-muted-foreground">此服务未开放日志读取。</p>}
          </section>
          {related.map((group) => <section key={group.owner} className="space-y-2">
            <h3 className="text-sm font-semibold">{group.label}</h3>
            {group.links.length ? <div className="flex flex-wrap gap-2">{group.links.map((link) =>
              <Button key={link.id} variant="outline" asChild><NavigationLink target={link} onClick={onClose}>{link.label}</NavigationLink></Button>
            )}</div> : <p className="text-sm text-muted-foreground">{group.emptyMessage}</p>}
          </section>)}
          <section className="space-y-2">
            <h3 className="text-sm font-semibold">仓库与项目资料</h3>
            <div className="flex flex-wrap gap-2">
              {repo && <Button variant="outline" asChild><a href={repo} target="_blank" rel="noopener noreferrer">{project ? '项目仓库' : '上游仓库'}</a></Button>}
              {project?.links.map((link) => <Button key={link.href} variant="outline" asChild><a href={link.href} target="_blank" rel="noopener noreferrer">{link.label}</a></Button>)}
            </div>
            {!project && <p className="text-xs text-muted-foreground">{repo ? '本地项目资料未登记，可先查看上游仓库。' : '尚未登记仓库或项目资料。'}</p>}
          </section>
        </div>}
      </SheetContent>
    </Sheet>
  );
}
