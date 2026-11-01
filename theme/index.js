import classicMui from './classic.mui.js';
import glassMui from './glass.mui.js';
import glowMui from './glow.mui.js';
import lightMui from './light.mui.js';

export const THEME_STORAGE_KEY = 'console-stack-theme';

export const themes = {
  light: {
    id: 'light',
    name: '日间浅色',
    description: '白底深字 · 清爽明亮',
    mui: lightMui,
  },
  classic: {
    id: 'classic',
    name: '经典暗色',
    description: '黑底白字 · GitHub 风格',
    mui: classicMui,
  },
  glass: {
    id: 'glass',
    name: '玻璃态',
    description: '背景模糊 · 透明层次',
    mui: glassMui,
  },
  glow: {
    id: 'glow',
    name: '霓虹光晕',
    description: '深色底 · 发光边框',
    mui: glowMui,
  },
};

export const themeList = Object.values(themes);
export const defaultThemeId = 'light';

export function getMuiTheme(themeId) {
  return themes[themeId]?.mui ?? themes[defaultThemeId].mui;
}

export function isValidThemeId(id) {
  return id in themes;
}

export function loadStoredThemeId() {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isValidThemeId(stored) ? stored : defaultThemeId;
  } catch {
    return defaultThemeId;
  }
}

export function saveThemeId(themeId) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, themeId);
  } catch {
    /* ignore */
  }
}
