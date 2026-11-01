/** 各主题强调色（用于 App.jsx 内联 sx） */
export function getThemeAccents(themeId) {
  if (themeId === 'light') {
    return {
      blue: '#0969da',
      pink: '#cf222e',
      green: '#1f883d',
      purple: '#8250df',
      body: '#1f2328',
      muted: '#656d76',
      faint: '#8b949e',
    };
  }
  if (themeId === 'classic') {
    return {
      blue: '#58a6ff',
      pink: '#f472b6',
      green: '#3fb950',
      purple: '#a371f7',
      body: '#f0f6fc',
      muted: '#8b949e',
      faint: '#6e7681',
    };
  }
  if (themeId === 'glow') {
    return {
      blue: '#7df9ff',
      pink: '#f5b8ff',
      green: '#7dffc8',
      purple: '#d8b4fe',
      body: '#f1f5f9',
      muted: '#e2e8f0',
      faint: '#cbd5e1',
    };
  }
  return {
    blue: '#dbeafe',
    pink: '#fce7f3',
    green: '#d1fae5',
    purple: '#ede9fe',
    body: '#ffffff',
    muted: 'rgba(255, 255, 255, 0.82)',
    faint: 'rgba(255, 255, 255, 0.7)',
  };
}
