import { createTheme } from '@mui/material/styles';

const glowSurface = {
  background: 'rgba(6, 6, 20, 0.72)',
  backdropFilter: 'blur(8px)',
  WebkitBackdropFilter: 'blur(8px)',
  border: '1px solid rgba(0, 229, 255, 0.35)',
  boxShadow:
    '0 0 24px rgba(0, 229, 255, 0.15), 0 0 48px rgba(168, 85, 247, 0.08), inset 0 0 30px rgba(0, 229, 255, 0.04)',
};

const sharedComponents = (surface, accent, rowHover, focusBorder) => ({
  MuiCssBaseline: {
    styleOverrides: {
      body: { backgroundColor: 'transparent' },
    },
  },
  MuiPaper: {
    styleOverrides: {
      root: { ...surface, backgroundImage: 'none' },
    },
  },
  MuiTableCell: {
    styleOverrides: {
      root: {
        borderColor: 'rgba(0, 229, 255, 0.18)',
        color: '#f1f5f9',
      },
      head: {
        color: '#7df9ff',
        fontWeight: 700,
        backgroundColor: 'rgba(0, 229, 255, 0.1)',
      },
    },
  },
  MuiTableRow: {
    styleOverrides: {
      root: { '&:hover': { backgroundColor: rowHover } },
    },
  },
  MuiTabs: {
    styleOverrides: {
      root: { minHeight: 48 },
      indicator: {
        backgroundColor: accent,
        height: 3,
        borderRadius: '3px 3px 0 0',
        boxShadow: '0 0 12px rgba(0, 229, 255, 0.6)',
      },
    },
  },
  MuiTab: {
    styleOverrides: {
      root: {
        color: 'rgba(255, 255, 255, 0.78)',
        textTransform: 'none',
        fontWeight: 500,
        fontSize: '0.9rem',
        minHeight: 48,
        '&.Mui-selected': {
          color: '#00e5ff',
          textShadow: '0 0 8px rgba(0, 229, 255, 0.5)',
        },
      },
    },
  },
  MuiDialog: {
    styleOverrides: {
      paper: { ...surface, borderRadius: 16 },
    },
  },
  MuiTextField: {
    styleOverrides: {
      root: {
        '& .MuiOutlinedInput-root': {
          backgroundColor: 'rgba(0, 0, 0, 0.35)',
          borderRadius: 10,
          '& fieldset': { borderColor: 'rgba(0, 229, 255, 0.2)' },
          '&:hover fieldset': { borderColor: 'rgba(0, 229, 255, 0.4)' },
          '&.Mui-focused fieldset': {
            borderColor: focusBorder,
            boxShadow: '0 0 12px rgba(0, 229, 255, 0.25)',
          },
        },
        '& .MuiInputLabel-root': { color: 'rgba(0, 229, 255, 0.65)' },
        '& .MuiInputBase-input': { color: '#ffffff' },
      },
    },
  },
  MuiChip: {
    styleOverrides: {
      root: {
        color: '#ffffff',
        fontWeight: 600,
        fontSize: '0.72rem',
      },
      filled: {
        backgroundColor: 'rgba(0, 229, 255, 0.18)',
        border: '1px solid rgba(0, 229, 255, 0.35)',
        boxShadow: '0 0 8px rgba(0, 229, 255, 0.18)',
        color: '#ffffff',
      },
      colorDefault: {
        backgroundColor: 'rgba(71, 85, 105, 0.65)',
        color: '#f8fafc',
        border: '1px solid rgba(148, 163, 184, 0.45)',
      },
      colorSuccess: {
        backgroundColor: 'rgba(0, 160, 90, 0.45)',
        color: '#ecfdf5',
        border: '1px solid rgba(52, 211, 153, 0.55)',
      },
      colorWarning: {
        backgroundColor: 'rgba(180, 120, 0, 0.45)',
        color: '#fffbeb',
        border: '1px solid rgba(251, 191, 36, 0.55)',
      },
      colorError: {
        backgroundColor: 'rgba(190, 40, 60, 0.45)',
        color: '#fff1f2',
        border: '1px solid rgba(248, 113, 113, 0.55)',
      },
    },
  },
  MuiCircularProgress: {
    styleOverrides: { root: { color: accent } },
  },
  MuiIconButton: {
    styleOverrides: {
      root: {
        color: '#00e5ff',
        '&:hover': {
          backgroundColor: 'rgba(0, 229, 255, 0.12)',
          boxShadow: '0 0 16px rgba(0, 229, 255, 0.3)',
        },
      },
    },
  },
  MuiMenu: {
    styleOverrides: {
      paper: { ...surface, borderRadius: 12 },
    },
  },
});

export default createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#00e5ff' },
    secondary: { main: '#e040fb' },
    success: { main: '#00ff88' },
    error: { main: '#ff4466' },
    warning: { main: '#ffaa00' },
    info: { main: '#00e5ff' },
    background: { default: 'transparent', paper: 'rgba(8, 8, 20, 0.92)' },
    text: { primary: '#f8fafc', secondary: '#e2e8f0' },
    divider: 'rgba(0, 229, 255, 0.15)',
  },
  typography: {
    fontFamily: '"Inter", "Roboto", system-ui, sans-serif',
  },
  components: sharedComponents(
    glowSurface,
    '#00e5ff',
    'rgba(0, 229, 255, 0.06)',
    'rgba(224, 64, 251, 0.6)',
  ),
});
