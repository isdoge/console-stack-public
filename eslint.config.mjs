// Root plugin sources share the frontend's exact lint rules and dependencies.
import frontend from './kernel/frontend/eslint.config.js';
export default frontend.map((config) => ({
  ...config,
  ...(config.files ? { files: config.files.map((pattern) => pattern.startsWith('../../') ? pattern.slice(6) : `kernel/frontend/${pattern}`) } : {}),
}));
