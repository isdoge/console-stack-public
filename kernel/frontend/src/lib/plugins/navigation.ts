import { useEffect, useMemo, useState } from 'react';
import { pluginIsAvailable, plugins } from './registry';
import { usePlugins } from './provider';
import type { NavigationContribution } from './navigation-types';

/** 只加载当前可见且可用的声明贡献；撤销状态立即隐藏，迟到结果不重新发布。 */
export function usePluginNavigation(active = true) {
  const { states } = usePlugins();
  const owners = plugins.filter((plugin) => plugin.navigation && states.some((state) =>
    state.id === plugin.id && state.enabled && state.visible && pluginIsAvailable(state)))
    .map((plugin) => plugin.id);
  const ownerKey = owners.join(',');
  const [loaded, setLoaded] = useState<{ key: string; groups: NavigationContribution[] }>({ key: '', groups: [] });
  useEffect(() => {
    setLoaded({ key: ownerKey, groups: [] });
    if (!active || !ownerKey) return;
    let cancelled = false;
    const selected = ownerKey ? ownerKey.split(',') : [];
    void Promise.all(selected.map(async (id) => {
      const plugin = plugins.find((candidate) => candidate.id === id)!;
      try {
        const navigation = await plugin.loadNavigation();
        return { ...navigation, owner: id };
      } catch {
        // 单个异步贡献失败不能阻止其它插件或框架导航。
        return null;
      }
    })).then((groups) => {
      if (!cancelled) setLoaded({ key: ownerKey, groups: groups.filter((group): group is NavigationContribution => group !== null) });
    });
    return () => { cancelled = true; };
  }, [active, ownerKey]);
  return useMemo(() => active && loaded.key === ownerKey ? loaded.groups : [], [active, loaded, ownerKey]);
}
