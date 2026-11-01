import { createTheme } from '@mui/material/styles';

const glassSurface = {
  background: 'rgba(255, 255, 255, 0.1)',
  backdropFilter: 'blur(20px)',
  WebkitBackdropFilter: 'blur(20px)',
  border: '1px solid rgba(255, 255, 255, 0.2)',
  boxShadow: '0 8px 32px rgba(31, 38, 135, 0.35)',
};

const sharedComponents = (surface, accent, rowHover) => ({
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
        borderColor: 'rgba(255, 255, 255, 0.14)',
        color: 'rgba(255, 255, 255, 0.92)',
      },
      head: {
        color: '#ffffff',
        fontWeight: 700,
        backgroundColor: 'rgba(0, 0, 0, 0.18)',
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
        '&.Mui-selected': { color: '#ffffff', fontWeight: 600 },
      },
    },
  },
  MuiDialog: {
    styleOverrides: {
      paper: { ...surface, borderRadius: 20 },
    },
  },
  MuiTextField: {
    styleOverrides: {
      root: {
        '& .MuiOutlinedInput-root': {
          backgroundColor: 'rgba(255, 255, 255, 0.05)',
          backdropFilter: 'blur(10px)',
          WebkitBackdropFilter: 'blur(10px)',
          borderRadius: 12,
          '& fieldset': { borderColor: 'rgba(255, 255, 255, 0.12)' },
          '&:hover fieldset': { borderColor: 'rgba(255, 255, 255, 0.25)' },
          '&.Mui-focused fieldset': { borderColor: 'rgba(255, 255, 255, 0.35)' },
        },
        '& .MuiInputLabel-root': { color: 'rgba(255, 255, 255, 0.6)' },
        '& .MuiInputBase-input': { color: '#ffffff' },
      },
    },
  },
  MuiChip: {
    styleOverrides: {
      root: {
        backdropFilter: 'blur(8px)',
        WebkitBackdropFilter: 'blur(8px)',
        color: '#ffffff',
        fontWeight: 500,
      },
      filled: {
        backgroundColor: 'rgba(255, 255, 255, 0.14)',
        border: '1px solid rgba(255, 255, 255, 0.2)',
        color: '#ffffff',
      },
      colorDefault: {
        backgroundColor: 'rgba(255, 255, 255, 0.14)',
        color: '#ffffff',
        border: '1px solid rgba(255, 255, 255, 0.22)',
      },
      colorSuccess: {
        backgroundColor: 'rgba(52, 211, 153, 0.22)',
        color: '#d1fae5',
        border: '1px solid rgba(52, 211, 153, 0.35)',
      },
      colorWarning: {
        backgroundColor: 'rgba(251, 191, 36, 0.22)',
        color: '#fef3c7',
        border: '1px solid rgba(251, 191, 36, 0.35)',
      },
      colorError: {
        backgroundColor: 'rgba(248, 113, 113, 0.22)',
        color: '#fee2e2',
        border: '1px solid rgba(248, 113, 113, 0.35)',
      },
    },
  },
  MuiCircularProgress: {
    styleOverrides: { root: { color: accent } },
  },
  MuiIconButton: {
    styleOverrides: {
      root: {
        color: 'rgba(255, 255, 255, 0.85)',
        '&:hover': { backgroundColor: 'rgba(255, 255, 255, 0.1)' },
      },
    },
  },
  MuiMenu: {
    styleOverrides: {
      paper: { ...surface, borderRadius: 16 },
    },
  },
});

export default createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#60a5fa' },
    secondary: { main: '#a78bfa' },
    success: { main: '#34d399' },
    error: { main: '#f87171' },
    warning: { main: '#fbbf24' },
    info: { main: '#38bdf8' },
    background: { default: 'transparent', paper: 'rgba(255, 255, 255, 0.1)' },
    text: { primary: '#ffffff', secondary: 'rgba(255, 255, 255, 0.88)' },
    divider: 'rgba(255, 255, 255, 0.12)',
  },
  typography: {
    fontFamily: '"Inter", "Roboto", system-ui, sans-serif',
  },
  components: sharedComponents(glassSurface, '#60a5fa', 'rgba(255, 255, 255, 0.06)'),
});
