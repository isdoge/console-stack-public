import { useEffect, useState } from 'react';

/**
 * T12 暗色模式：三态偏好（light / dark / system），持久到 localStorage，
 * 解析结果落到 documentElement 的 `dark` class（index.css 的 .dark 变量组生效）。
 * index.html 里有同口径的内联脚本提前加 class，避免首帧闪白。
 */
export type ThemePref = 'light' | 'dark' | 'system';

export const THEME_KEY = 'console.theme';

function readPref(): ThemePref {
  try {
    const raw = localStorage.getItem(THEME_KEY);
    return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'system';
  } catch {
    return 'system';
  }
}

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  } catch {
    return false;
  }
}

export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(readPref);
  const [systemDark, setSystemDark] = useState(systemPrefersDark);

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const resolved: 'light' | 'dark' = pref === 'system' ? (systemDark ? 'dark' : 'light') : pref;

  useEffect(() => {
    document.documentElement.classList.toggle('dark', resolved === 'dark');
    try {
      localStorage.setItem(THEME_KEY, pref);
    } catch {
      // localStorage 不可用（隐私模式等）时退化为会话内记忆
    }
  }, [pref, resolved]);

  const toggle = () => setPref(resolved === 'dark' ? 'light' : 'dark');

  return { pref, resolved, toggle };
}
