#!/usr/bin/env node
/**
 * 根据组合边界输入（CONSOLE_COMPOSITION_FILE / CONSOLE_PLUGIN_ROOT / CONSOLE_INSTANCE_PROFILE）
 * 校验前端插件来源并生成临时 TypeScript 配置（默认 kernel/frontend/.tsconfig.composition.json）。
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from '../kernel/frontend/node_modules/typescript/lib/typescript.js';
import {
  DEFAULT_FRONTEND_ROOT,
  prepareFrontendComposition,
} from '../kernel/frontend/src/lib/plugins/plugin-build-validation.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');

function parseArgs(argv) {
  let outPath;
  let json = false;
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--out' || arg === '--tsconfig') {
      const next = argv[i + 1];
      if (!next) throw new Error(`${arg} 需要路径参数`);
      outPath = next;
      i += 1;
    } else if (arg === '--json') {
      json = true;
    } else {
      throw new Error(`未知参数：${arg}`);
    }
  }
  return { outPath, json };
}

async function main() {
  const { outPath, json } = parseArgs(process.argv.slice(2));
  const result = await prepareFrontendComposition({
    env: process.env,
    frontendRoot: DEFAULT_FRONTEND_ROOT,
    cwd: process.cwd(),
    outPath,
    ts,
  });
  if (json) {
    process.stdout.write(
      `${JSON.stringify({
        ok: true,
        profile: result.composition.profile,
        source: result.composition.source,
        pluginsRoot: result.composition.pluginsRoot,
        declaredPlugins: result.composition.declaredPlugins,
        tsconfigPath: path.relative(REPO_ROOT, result.tsconfigPath) || result.tsconfigPath,
      })}\n`,
    );
  }
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exit(1);
});
