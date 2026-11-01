import { useState } from 'react';
import { AlertCircle, Puzzle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { usePlugins } from './provider';
import { pluginIsAvailable, type PluginState } from './registry';

export function PluginManager() {
  const { states, loading, error, reload, update } = usePlugins();
  const [busy, setBusy] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const change = async (plugin: PluginState, field: 'enabled' | 'visible') => {
    setBusy(plugin.id); setSaveError(null);
    try { await update(plugin.id, { [field]: !plugin[field] }); }
    catch (err) { setSaveError(err instanceof Error ? err.message : '保存失败'); }
    finally { setBusy(null); }
  };
  const retry = async (plugin: PluginState) => {
    setBusy(plugin.id); setSaveError(null);
    try { await update(plugin.id, { enabled: true }); }
    catch (err) { setSaveError(err instanceof Error ? err.message : '重试失败'); }
    finally { setBusy(null); }
  };
  return <div className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="flex items-center gap-2 text-lg font-semibold"><Puzzle className="h-5 w-5" />功能插件</h2>
        <p className="mt-1 text-sm text-muted-foreground">按需启用功能。停用保留数据；隐藏入口后，直达链接仍可使用。</p></div>
      <Button variant="outline" size="sm" disabled={Boolean(busy) || loading} onClick={() => void reload()}><RefreshCw className="mr-2 h-4 w-4" />刷新</Button>
    </div>
    {(error || saveError) && <p role="alert" className="flex items-center gap-2 text-sm text-destructive"><AlertCircle className="h-4 w-4" />{saveError || error}</p>}
    {loading && <p className="text-sm text-muted-foreground">正在读取插件…</p>}
    <div className="grid gap-3 lg:grid-cols-2">{states.map((plugin) => <Card key={plugin.id}>
      <CardContent className="space-y-4 p-4">
        <div><div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">{plugin.name}</h3><span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">{!plugin.enabled ? '已停用' : !pluginIsAvailable(plugin) ? '运行故障' : plugin.visible ? '运行中' : '运行中 · 入口隐藏'}</span></div>
          <p className="mt-2 text-sm text-muted-foreground">{plugin.description}</p></div>
        {plugin.enabled && !pluginIsAvailable(plugin) && <p className="text-sm text-destructive" role="status">{plugin.error || '插件暂不可用，宿主已暂停。'}</p>}
        {plugin.enabled && !pluginIsAvailable(plugin) && <Button type="button" size="sm" variant="outline" disabled={Boolean(busy)} onClick={() => void retry(plugin)}>重新启用并重试</Button>}
        <div className="flex flex-wrap gap-4">
          <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm"><input type="checkbox" role="switch" aria-label={`启用${plugin.name}`} checked={plugin.enabled} disabled={Boolean(busy)} onChange={() => void change(plugin, 'enabled')} className="h-4 w-4 accent-primary" />启用功能</label>
          <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm"><input type="checkbox" role="switch" aria-label={`显示${plugin.name}入口`} checked={plugin.visible} disabled={Boolean(busy) || !plugin.enabled} onChange={() => void change(plugin, 'visible')} className="h-4 w-4 accent-primary" />显示入口</label>
        </div>
        <p className="text-xs text-muted-foreground">{busy === plugin.id ? '正在保存…' : `版本 ${plugin.version}`}</p>
      </CardContent></Card>)}</div>
  </div>;
}
