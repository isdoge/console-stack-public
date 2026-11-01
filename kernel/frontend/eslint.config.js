import path from 'node:path';
import { fileURLToPath } from 'node:url';
import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import { resolveEslintPluginFiles } from './src/lib/plugins/plugin-build-validation.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginFiles = resolveEslintPluginFiles({ frontendRoot: __dirname });

export default tseslint.config(
  { ignores: ['dist', 'node_modules'] },
  {
    files: ['src/**/*.{ts,tsx}', ...pluginFiles],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2020,
      globals: { ...globals.browser, ...globals.es2020 },
    },
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // 存量代码大量使用 any 与宽松类型，门禁先聚焦真实缺陷类规则
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },
  {
    // vitest 全局（test/expect 等）
    files: ['src/**/*.test.{ts,tsx}', 'src/setupTests.ts'],
    languageOptions: {
      globals: { ...globals.node },
    },
  }
);
