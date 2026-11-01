import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import ts from 'typescript';
import packageJson from './package.json';
import {
  readBuildIdentityFile,
  resolveFrontendComposition,
  resolveVitestInclude,
  validatePluginSources,
} from './src/lib/plugins/plugin-build-validation.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function validatePluginRegistry(pluginsRoot: string) {
  let validated: Promise<unknown> | null = null;
  const runValidation = () => {
    if (!validated) {
      validated = (async () => {
        const identity = readBuildIdentityFile();
        const manifests = await validatePluginSources(pluginsRoot, ts);
        if (identity) {
          const expected = manifests.map((m) => `${m.id}@${m.version}`).sort().join(',');
          const actual = (identity.plugins as Array<{ id: string; version: string }>)
            .map((p) => `${p.id}@${p.version}`)
            .sort()
            .join(',');
          if (expected !== actual) {
            throw new Error(`组合构建身份插件列表与插件根不一致：identity=${actual} actual=${expected}`);
          }
        }
      })();
    }
    return validated;
  };

  return {
    name: 'validate-plugin-registry',
    enforce: 'pre' as const,
    async configResolved() {
      await runValidation();
    },
    async buildStart(this: { error?: (message: string) => never } | void) {
      try {
        await runValidation();
      } catch (error) {
        const message = error instanceof Error ? error.message : '插件清单或前端入口校验失败';
        if (this && typeof this.error === 'function') this.error(message);
        throw error;
      }
    },
    generateBundle(this: {
      emitFile: (file: { type: 'asset'; fileName: string; source: string }) => void;
      error?: (message: string) => never;
    }) {
      try {
        const identity = readBuildIdentityFile();
        if (identity) {
          this.emitFile({
            type: 'asset',
            fileName: 'build-identity.json',
            source: `${JSON.stringify(identity, null, 2)}\n`,
          });
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : '构建身份文件校验失败';
        if (typeof this.error === 'function') this.error(message);
        throw error;
      }
    },
  };
}

export default defineConfig(() => {
  const composition = resolveFrontendComposition({ frontendRoot: __dirname });
  const allowDirs = Array.from(new Set([path.resolve(__dirname, '../..'), composition.pluginsRoot]));
  // B7-3：插件测试位于 frontend 根之外（plugins/<id>/frontend/**）。Node/Vite 从
  // 该文件向上找 node_modules，只会找到仓库根那份，而测试依赖（@testing-library、
  // vitest 等）只装在本目录，于是插件测试解析失败。`test.alias` 把测试期才有意义
  // 的那几个包显式指到本目录，不改构建产物、也不给插件目录造第二份安装。
  const testAlias = Object.fromEntries(
    ['@testing-library/react', '@testing-library/jest-dom', '@testing-library/user-event',
     '@testing-library/dom']
      .filter((name) => existsSync(path.resolve(__dirname, 'node_modules', name)))
      .map((name) => [name, path.resolve(__dirname, 'node_modules', name)]),
  );
  return {
    plugins: [validatePluginRegistry(composition.pluginsRoot), react()],
    resolve: {
      dedupe: Object.keys(packageJson.dependencies),
      alias: {
        '@plugins': composition.pluginsRoot,
        '@': path.resolve(__dirname, 'src'),
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
    },
    test: {
      environment: 'jsdom',
      include: resolveVitestInclude({ frontendRoot: __dirname }),
      globals: true,
      setupFiles: ['src/setupTests.ts'],
      alias: testAlias,
    },
    server: {
      fs: { allow: allowDirs },
      port: 3000,
      proxy: {
        '/api': {
          target: 'http://localhost:8080',
          changeOrigin: true,
        },
        '/beszel': {
          target: 'http://localhost:8090',
          changeOrigin: true,
        },
      },
    },
  };
});
