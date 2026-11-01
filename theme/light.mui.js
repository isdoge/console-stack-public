import { createTheme } from '@mui/material/styles';

const lightSurface = {
  background: '#ffffff',
  backgroundImage: 'none',
  border: '1px solid #d0d7de',
  boxShadow: '0 1px 3px rgba(31, 35, 40, 0.06)',
};

export default createTheme({
  palette: {
    mode: 'light',
    primary: { main: '#0969da' },
    secondary: { main: '#8250df' },
    success: { main: '#1f883d' },
    error: { main: '#cf222e' },
    warning: { main: '#bf8700' },
    info: { main: '#0969da' },
    background: { default: '#f6f8fa', paper: '#ffffff' },
    text: { primary: '#1f2328', secondary: '#656d76' },
    divider: '#d0d7de',
  },
  typography: {
    fontFamily: '"Inter", "Roboto", system-ui, sans-serif',
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: { backgroundColor: '#f6f8fa' },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: { ...lightSurface },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: {
          borderColor: '#d0d7de',
          color: '#1f2328',
        },
        head: {
          color: '#1f2328',
          fontWeight: 700,
          backgroundColor: '#f6f8fa',
        },
      },
    },
    MuiTableRow: {
      styleOverrides: {
        root: {
          '&:hover': { backgroundColor: '#f3f4f6' },
        },
      },
    },
    MuiTabs: {
      styleOverrides: {
        root: { minHeight: 48 },
        indicator: {
          backgroundColor: '#0969da',
          height: 2,
        },
      },
    },
    MuiTab: {
      styleOverrides: {
        root: {
          color: '#656d76',
          textTransform: 'none',
          fontWeight: 500,
          fontSize: '0.9rem',
          minHeight: 48,
          '&.Mui-selected': { color: '#1f2328', fontWeight: 600 },
        },
      },
    },
    MuiDialog: {
      styleOverrides: {
        paper: { ...lightSurface, borderRadius: 12 },
      },
    },
    MuiTextField: {
      styleOverrides: {
        root: {
          '& .MuiOutlinedInput-root': {
            backgroundColor: '#ffffff',
            borderRadius: 6,
            '& fieldset': { borderColor: '#d0d7de' },
            '&:hover fieldset': { borderColor: '#afb8c1' },
            '&.Mui-focused fieldset': { borderColor: '#0969da' },
          },
          '& .MuiInputLabel-root': { color: '#656d76' },
          '& .MuiInputBase-input': { color: '#1f2328' },
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: { fontWeight: 500, fontSize: '0.72rem' },
        colorDefault: {
          backgroundColor: '#f3f4f6',
          color: '#656d76',
          border: '1px solid #d0d7de',
        },
        colorSuccess: {
          backgroundColor: '#dafbe1',
          color: '#1a7f37',
          border: '1px solid #4ac26b',
        },
        colorWarning: {
          backgroundColor: '#fff8c5',
          color: '#9a6700',
          border: '1px solid #d4a72c',
        },
        colorError: {
          backgroundColor: '#ffebe9',
          color: '#cf222e',
          border: '1px solid #ff8182',
        },
      },
    },
    MuiCircularProgress: {
      styleOverrides: { root: { color: '#0969da' } },
    },
    MuiIconButton: {
      styleOverrides: {
        root: {
          color: '#656d76',
          '&:hover': { backgroundColor: '#f3f4f6' },
        },
      },
    },
    MuiMenu: {
      styleOverrides: {
        paper: { ...lightSurface, borderRadius: 8 },
      },
    },
  },
});
