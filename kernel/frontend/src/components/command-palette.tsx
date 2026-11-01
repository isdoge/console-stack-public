import { plugins, pluginForPath, pluginIsAvailable } from '@/lib/plugins/registry';
import { usePlugins } from '@/lib/plugins/provider';
import { usePluginNavigation } from '@/lib/plugins/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CornerDownLeft, Loader2, Search } from 'lucide-react';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { listMachines, listServices } from '@/lib/api';
import { cn } from '@/lib/utils';
import type { Machine, Service } from '@/lib/types';

interface PaletteItem {
  key: string;
  group: string;
  label: string;
  hint?: string;
  to: string;
  external?: boolean;
}

const STATIC_PAGES: PaletteItem[] = [
  { key: 'page-dash', group: '页面', label: '总控台', to: '/' },
  { key: 'page-devices', group: '页面', label: '设备与服务', to: '/devices' },
  ...plugins.filter((p) => !p.persistent).map((p) => ({ key: `page-${p.id}`, group: '页面', label: p.name, to: p.routes[0] })),
  { key: 'page-settings', group: '页面', label: '设置', to: '/settings' },
];

function buildItems(machines: Machine[], services: Service[]): PaletteItem[] {
  const machineItems: PaletteItem[] = machines.map((m) => ({
    key: `machine-${m.id}`, group: '机器', label: m.label,
    hint: m.role === 'node' ? '节点' : '服务器', to: `/devices/${m.id}`,
  }));
  const serviceItems: PaletteItem[] = services.map((s) => ({
    key: `service-${s.machine}-${s.name}`, group: '服务', label: s.name,
    hint: s.machine, to: `/devices/${s.machine}`,
  }));
  return [...STATIC_PAGES, ...machineItems, ...serviceItems];
}

function matchQuery(item: PaletteItem, q: string): boolean {
  const hay = `${item.label} ${item.hint || ''} ${item.group}`.toLowerCase();
  return q.toLowerCase().split(/\s+/).every((token) => hay.includes(token));
}

interface Props { openSignal?: number }

export function CommandPalette({ openSignal = 0 }: Props) {
  const navigate = useNavigate();
  const { states } = usePlugins();
  const [open, setOpen] = useState(false);
  const navigation = usePluginNavigation(open);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<PaletteItem[]>(STATIC_PAGES);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const groups = useMemo(() => [...new Set(['页面', '机器', ...navigation.map((group) => group.commandGroup), '服务'])], [navigation]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setOpen((value) => !value);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => { if (openSignal > 0) setOpen(true); }, [openSignal]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    void Promise.all([listMachines().catch(() => []), listServices().catch(() => [])])
      .then(([machines, services]) => { if (!cancelled) setItems(buildItems(machines, services)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open]);

  const filtered = useMemo(() => {
    const visibleItems = items.filter((item) => {
      const plugin = pluginForPath(item.to.split('?')[0]);
      return !plugin || states.some((state) => state.id === plugin.id && state.enabled && state.visible && pluginIsAvailable(state));
    });
    const contributed: PaletteItem[] = navigation.flatMap((group) => group.links.map((link) => ({
      key: `${group.owner}-${link.id}`, group: group.commandGroup, label: link.label,
      hint: link.machine, to: link.to, external: link.external,
    })));
    const all = [...visibleItems.filter((item) => item.group !== '服务'), ...contributed, ...visibleItems.filter((item) => item.group === '服务')];
    if (!query.trim()) return all.slice(0, 30);
    return all.filter((item) => matchQuery(item, query))
      .sort((a, b) => groups.indexOf(a.group) - groups.indexOf(b.group)).slice(0, 30);
  }, [items, navigation, query, states, groups]);

  useEffect(() => setActive(0), [query, open, filtered]);

  const go = (item: PaletteItem) => {
    setOpen(false);
    setQuery('');
    if (item.external) window.open(item.to, '_blank', 'noopener,noreferrer');
    else navigate(item.to);
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((index) => Math.min(index + 1, filtered.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((index) => Math.max(index - 1, 0)); }
    else if (e.key === 'Enter' && filtered[active]) { e.preventDefault(); go(filtered[active]); }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-lg p-0" onKeyDown={onKeyDown}>
        <DialogTitle className="sr-only">快速跳转</DialogTitle>
        <div className="flex items-center gap-2 border-b px-3">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input ref={inputRef} value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder={`搜索${groups.join(' / ')}…`} className="h-11 w-full bg-transparent text-sm outline-none" />
          {loading && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-muted-foreground" />}
          <kbd className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">Esc</kbd>
        </div>
        <div className="max-h-80 overflow-y-auto p-1.5">
          {filtered.length === 0 ? <p className="px-3 py-6 text-center text-sm text-muted-foreground">没有匹配项</p> : groups.map((group) => {
            const groupItems = filtered.filter((item) => item.group === group);
            if (groupItems.length === 0) return null;
            return <div key={group} className="mb-1">
              <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{group}</div>
              {groupItems.map((item) => {
                const index = filtered.indexOf(item);
                return <button key={item.key} type="button"
                  className={cn('flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm outline-none',
                    index === active ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/60')}
                  onMouseEnter={() => setActive(index)} onClick={() => go(item)}>
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                  {item.hint && <span className="shrink-0 text-xs text-muted-foreground">{item.hint}</span>}
                  {index === active && <CornerDownLeft className="h-3 w-3 shrink-0 text-muted-foreground" />}
                </button>;
              })}
            </div>;
          })}
        </div>
      </DialogContent>
    </Dialog>
  );
}
