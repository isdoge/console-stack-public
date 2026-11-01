import { createTheme } from '@mui/material/styles';

const classicSurface = {
  background: '#161b22',
  backgroundImage: 'none',
  border: '1px solid #30363d',
  boxShadow: 'none',
};

export default createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#58a6ff' },
    secondary: { main: '#a371f7' },
    success: { main: '#3fb950' },
    error: { main: '#f85149' },
    warning: { main: '#d29922' },
    info: { main: '#58a6ff' },
    background: { default: '#0d1117', paper: '#161b22' },
    text: { primary: '#f0f6fc', secondary: '#c9d1d9' },
    divider: '#30363d',
  },
  typography: {
    fontFamily: '"Inter", "Roboto", system-ui, sans-serif',
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: { backgroundColor: '#0d1117' },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: { ...classicSurface },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: {
          borderColor: '#30363d',
          color: '#c9d1d9',
        },
        head: {
          color: '#f0f6fc',
          fontWeight: 700,
          backgroundColor: '#0d1117',
        },
      },
    },
    MuiTableRow: {
      styleOverrides: {
        root: {
          '&:hover': { backgroundColor: '#21262d' },
        },
      },
    },
    MuiTabs: {
      styleOverrides: {
        root: { minHeight: 48 },
        indicator: {
          backgroundColor: '#58a6ff',
          height: 2,
        },
      },
    },
    MuiTab: {
      styleOverrides: {
        root: {
          color: '#8b949e',
          textTransform: 'none',
          fontWeight: 500,
          fontSize: '0.9rem',
          minHeight: 48,
          '&.Mui-selected': { color: '#f0f6fc' },
        },
      },
    },
    MuiDialog: {
      styleOverrides: {
        paper: { ...classicSurface, borderRadius: 6 },
      },
    },
    MuiTextField: {
      styleOverrides: {
        root: {
          '& .MuiOutlinedInput-root': {
            backgroundColor: '#0d1117',
            borderRadius: 6,
            '& fieldset': { borderColor: '#30363d' },
            '&:hover fieldset': { borderColor: '#8b949e' },
            '&.Mui-focused fieldset': { borderColor: '#58a6ff' },
          },
          '& .MuiInputLabel-root': { color: '#8b949e' },
          '& .MuiInputBase-input': { color: '#f0f6fc' },
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: { fontWeight: 500, fontSize: '0.72rem' },
        colorDefault: {
          backgroundColor: '#21262d',
          color: '#c9d1d9',
          border: '1px solid #30363d',
        },
        colorSuccess: {
          backgroundColor: 'rgba(46, 160, 67, 0.15)',
          color: '#3fb950',
          border: '1px solid rgba(46, 160, 67, 0.4)',
        },
        colorWarning: {
          backgroundColor: 'rgba(210, 153, 34, 0.15)',
          color: '#d29922',
          border: '1px solid rgba(210, 153, 34, 0.4)',
        },
        colorError: {
          backgroundColor: 'rgba(248, 81, 73, 0.15)',
          color: '#f85149',
          border: '1px solid rgba(248, 81, 73, 0.4)',
        },
      },
    },
    MuiCircularProgress: {
      styleOverrides: { root: { color: '#58a6ff' } },
    },
    MuiIconButton: {
      styleOverrides: {
        root: {
          color: '#c9d1d9',
          '&:hover': { backgroundColor: '#21262d' },
        },
      },
    },
    MuiMenu: {
      styleOverrides: {
        paper: { ...classicSurface, borderRadius: 6 },
      },
    },
  },
});
