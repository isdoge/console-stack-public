import { execFile } from 'node:child_process';
import { mkdtemp, mkdir, readFile, realpath, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import tailwindcssAnimate from 'tailwindcss-animate';
import ts from 'typescript';
import { build, createServer, type ViteDevServer } from 'vite';
import { afterEach, describe, expect, test } from 'vitest';
import {
  DEFAULT_FRONTEND_ROOT,
  prepareFrontendComposition,
  readBuildIdentityFile,
  resolveFrontendComposition,
  resolveTailwindContent,
  resolveVitestInclude,
  validatePluginSources,
} from './plugin-build-validation.mjs';

const execFileAsync = promisify(execFile);
const VITE_CONFIG_PATH = path.resolve(DEFAULT_FRONTEND_ROOT, 'vite.config.ts');
const PREPARE_SCRIPT_PATH = path.resolve(DEFAULT_FRONTEND_ROOT, '../../scripts/prepare_frontend_composition.mjs');

const temporaryRoots: string[] = [];
const activeServers: ViteDevServer[] = [];
const SAVED_ENV_KEYS = [
  'CONSOLE_COMPOSITION_FILE',
  'CONSOLE_PLUGIN_ROOT',
  'CONSOLE_INSTANCE_PROFILE',
  'CONSOLE_BUILD_IDENTITY_FILE',
] as const;

afterEach(async () => {
  for (const key of SAVED_ENV_KEYS) {
    delete process.env[key];
  }
  await Promise.all(activeServers.splice(0).map((server) => server.close()));
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function fixture(
  manifests: Array<Record<string, unknown>>,
  entries: Record<string, string>,
  options: { dirName?: string; contractPatch?: Record<string, unknown> } = {},
) {
  const base = await realpath(await mkdtemp(path.join(os.tmpdir(), 'console-plugin-build-')));
  temporaryRoots.push(base);
  const root = options.dirName ? path.join(base, options.dirName) : base;
  await mkdir(root, { recursive: true });
  await mkdir(path.join(base, 'config'), { recursive: true });
  const baseComposition = resolveFrontendComposition({ env: {} });
  const contract = {
    ...baseComposition.contract,
    ...(options.contractPatch ?? {}),
  };
  await writeFile(path.join(root, 'contract.json'), JSON.stringify(contract));
  await writeFile(path.join(root, 'composition.json'), JSON.stringify({ plugins: manifests.map((m) => String(m.id)) }));
  await Promise.all(
    manifests.map(async (item) => {
      const id = String(item.id);
      const directory = path.join(root, id);
      await mkdir(path.join(directory, 'frontend'), { recursive: true });
      await writeFile(path.join(directory, 'manifest.json'), JSON.stringify(item));
      await writeFile(path.join(directory, 'frontend/index.tsx'), entries[id] ?? 'export default function Page() { return null; }');
    }),
  );
  return { base, root };
}

function manifest(id: string, patch: Record<string, unknown> = {}) {
  return {
    id,
    name: id,
    description: id,
    version: '1',
    api_version: 1,
    owns: { tables: [], settings: [], files: [], tasks: [] },
    routes: [`/${id}`],
    api_prefixes: [`/api/${id}`],
    ...patch,
  };
}

function compositionFilePayload(pluginsPath: string, profile: 'private' | 'public' = 'private', extra: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    profile,
    roots: {
      config: './config',
      data: './data',
      static: './static',
      backup: './backup',
      plugins: pluginsPath,
    },
    host: {},
    alert_machines: {},
    ...extra,
  };
}

describe('Node build-time plugin validation & composition boundary', () => {
  test('parses the real plugin manifests from resolved composition root and checks actual TSX exports', async () => {
    const resolved = resolveFrontendComposition({ env: {} });
    const plugins = await validatePluginSources(resolved.pluginsRoot, ts);
    expect(plugins.map((plugin) => plugin.id)).toEqual([
      'audit',
      'backup_evidence',
      'feed',
      'ledger',
      'panels',
      'personal_workbench',
      'renewals',
    ]);
  });

  test('fails the build when a declared component export is absent', async () => {
    const { root } = await fixture([manifest('sample', { sidebar: true })], {
      sample: 'export default function Page() { return null; }',
    });
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/声明了 Sidebar，但入口未导出/);
  });

  test('requires the declared navigation loader as a value export', async () => {
    const { root } = await fixture([manifest('sample', { navigation: true })], {
      sample: 'export default function Page() { return null; } export type { loadNavigation } from "./types";',
    });
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/loadNavigation/);
    await writeFile(path.join(root, 'sample/frontend/index.tsx'),
      'export default function Page() { return null; } export { loadNavigation } from "./navigation";');
    await expect(validatePluginSources(root, ts)).resolves.toHaveLength(1);
  });

  test('ignores non-plugin cache directories and rejects type-only exports', async () => {
    const { root } = await fixture([manifest('sample', { sidebar: true })], {
      sample: 'export default function Page() { return null; } export type { Sidebar } from "./types";',
    });
    await mkdir(path.join(root, '__pycache__'));
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/Sidebar/);
    await writeFile(
      path.join(root, 'sample/frontend/index.tsx'),
      'export default function Page() { return null; } export function Sidebar() { return null; }',
    );
    await expect(validatePluginSources(root, ts)).resolves.toHaveLength(1);
  });

  test('fails duplicate resource ownership across plugins', async () => {
    const owns = { tables: ['owned_table'], settings: [], files: [], tasks: [] };
    const { root } = await fixture([manifest('first', { owns }), manifest('second', { owns })], {});
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/所有权冲突/);
  });

  test('rejects a plugin directory that the composition manifest does not list', async () => {
    const { root } = await fixture([manifest('listed')], {});
    await mkdir(path.join(root, 'ghost/frontend'), { recursive: true });
    await writeFile(path.join(root, 'ghost/manifest.json'), JSON.stringify(manifest('ghost')));
    await writeFile(path.join(root, 'ghost/frontend/index.tsx'), 'export default function Page() { return null; }');
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/未在组合清单登记/);
  });

  test('rejects a composition entry with no matching plugin directory', async () => {
    const { root } = await fixture([manifest('listed')], {});
    await writeFile(path.join(root, 'composition.json'), JSON.stringify({ plugins: ['listed', 'ghost'] }));
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/不存在的插件/);
  });

  test('rejects a malformed or missing composition manifest', async () => {
    const { root } = await fixture([manifest('listed')], {});
    await writeFile(path.join(root, 'composition.json'), JSON.stringify({ plugins: ['listed', 'listed'] }));
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/重复/);
    await rm(path.join(root, 'composition.json'));
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/插件组合清单/);
  });

  test('rejects any symlink inside plugins root even when target has no manifest.json or is broken', async () => {
    const { base, root } = await fixture([manifest('listed')], {}, { dirName: 'real_root' });
    const outsideDirWithoutManifest = path.join(base, 'outside_no_manifest');
    await mkdir(outsideDirWithoutManifest, { recursive: true });
    await symlink(outsideDirWithoutManifest, path.join(root, 'sneaky_link'));
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/不能是符号链接/);

    await rm(path.join(root, 'sneaky_link'));
    await symlink(path.join(base, 'non_existent_target'), path.join(root, 'broken_link'));
    await expect(validatePluginSources(root, ts)).rejects.toThrow(/不能是符号链接/);
  });

  test('resolves external plugins root from CONSOLE_COMPOSITION_FILE and gives CONSOLE_PLUGIN_ROOT priority', async () => {
    const { base, root: extFromFile } = await fixture([manifest('ext_alpha')], {}, { dirName: 'custom_ext_mods' });
    const { root: extFromEnv } = await fixture([manifest('ext_beta')], {}, { dirName: 'override_mods' });
    const instanceDir = path.join(base, 'instance/private');
    await mkdir(path.join(instanceDir, 'config'), { recursive: true });
    const compFile = path.join(instanceDir, 'composition.json');
    await writeFile(
      compFile,
      JSON.stringify(compositionFilePayload(path.relative(instanceDir, extFromFile), 'public', { name: 'pub-test' })),
    );

    const fromFile = resolveFrontendComposition({
      env: { CONSOLE_COMPOSITION_FILE: compFile },
    });
    expect(fromFile.profile).toBe('public');
    expect(fromFile.compositionName).toBe('pub-test');
    expect(fromFile.pluginsRoot).toBe(extFromFile);
    expect(fromFile.declaredPlugins).toEqual(['ext_alpha']);
    expect(fromFile.source).toBe('composition_file');

    const fromEnvOverride = resolveFrontendComposition({
      env: {
        CONSOLE_COMPOSITION_FILE: compFile,
        CONSOLE_PLUGIN_ROOT: extFromEnv,
      },
    });
    expect(fromEnvOverride.profile).toBe('public');
    expect(fromEnvOverride.pluginsRoot).toBe(extFromEnv);
    expect(fromEnvOverride.declaredPlugins).toEqual(['ext_beta']);
    expect(fromEnvOverride.source).toBe('env');
  });

  test('strictly parses instance composition, requiring the backend schema and rejecting duplicate keys', async () => {
    const { base, root } = await fixture([manifest('sample')], {}, { dirName: 'plugins' });
    const compFile = path.join(base, 'instance.json');
    const writeComposition = async (raw: string) => writeFile(compFile, raw);
    const env = { CONSOLE_COMPOSITION_FILE: compFile };
    const valid = compositionFilePayload('./plugins');

    await writeComposition(JSON.stringify(valid).replace('"schema_version":1', '"schema_version":1,"schema_version":1'));
    expect(() => resolveFrontendComposition({ env })).toThrow(/重复键/);

    const { roots: _roots, ...missingRoots } = valid;
    await writeComposition(JSON.stringify(missingRoots));
    expect(() => resolveFrontendComposition({ env })).toThrow(/roots/);

    const { host: _host, ...missingHost } = valid;
    await writeComposition(JSON.stringify(missingHost));
    expect(() => resolveFrontendComposition({ env })).toThrow(/host/);

    const { alert_machines: _alerts, ...missingAlerts } = valid;
    await writeComposition(JSON.stringify(missingAlerts));
    expect(() => resolveFrontendComposition({ env })).toThrow(/alert_machines/);

    await writeComposition(JSON.stringify(valid));
    expect(resolveFrontendComposition({ env }).pluginsRoot).toBe(root);

    await writeComposition(JSON.stringify({ ...valid, host: { local_machine: 'BAD ID' } }));
    expect(() => resolveFrontendComposition({ env })).toThrow(/host.local_machine/);
    await writeComposition(JSON.stringify({ ...valid, host: null }));
    expect(() => resolveFrontendComposition({ env })).toThrow(/host 必须是对象/);
    await writeComposition(JSON.stringify({ ...valid, host: { home_lan_ip: 'not-an-ip' } }));
    expect(() => resolveFrontendComposition({ env })).toThrow(/home_lan_ip/);
    await writeComposition(JSON.stringify({ ...valid, host: { local_machine: null } }));
    expect(resolveFrontendComposition({ env }).profile).toBe('private');
    await writeComposition(JSON.stringify({ ...valid, host: { extra: true } }));
    expect(() => resolveFrontendComposition({ env })).toThrow(/host 含未知字段/);
    await writeComposition(JSON.stringify({ ...valid, alert_machines: { nas: { display: 'NAS', load5_threshold: 0 } } }));
    expect(() => resolveFrontendComposition({ env })).toThrow(/load5_threshold/);
    await writeComposition(JSON.stringify({ ...valid, alert_machines: { 'BAD ID': { display: 'NAS', load5_threshold: 1 } } }));
    expect(() => resolveFrontendComposition({ env })).toThrow(/alert_machines/);
  });

  test('uses the same accepted host and alert inputs as the backend', async () => {
    const cases = JSON.parse(await readFile(path.resolve(DEFAULT_FRONTEND_ROOT, '../testdata/composition-protocol.json'), 'utf8'));
    const { base } = await fixture([manifest('sample')], {}, { dirName: 'plugins' });
    const compositionFile = path.join(base, 'instance.json');
    for (const item of cases) {
      await writeFile(compositionFile, JSON.stringify(compositionFilePayload('./plugins', 'private', item.patch)));
      const resolve = () => resolveFrontendComposition({ env: { CONSOLE_COMPOSITION_FILE: compositionFile } });
      if (item.accepted) expect(resolve, item.name).not.toThrow();
      else expect(resolve, item.name).toThrow();
    }
  });

  test('rejects symlinked non-plugin roots and cancelled absolute symlink paths', async () => {
    const { base, root } = await fixture([manifest('sample')], {}, { dirName: 'plugins' });
    const compFile = path.join(base, 'instance.json');
    const linked = path.join(base, 'linked');
    await symlink(root, linked);
    const valid = compositionFilePayload('./plugins');
    for (const key of ['data', 'static', 'backup']) {
      await writeFile(compFile, JSON.stringify({ ...valid, roots: { ...valid.roots, [key]: './linked' } }));
      expect(() => resolveFrontendComposition({ env: { CONSOLE_COMPOSITION_FILE: compFile } })).toThrow(/符号链接/);
    }
    expect(() => resolveFrontendComposition({ env: { CONSOLE_PLUGIN_ROOT: `${linked}/../plugins` } })).toThrow(/符号链接/);
    await writeFile(compFile, JSON.stringify(valid));
    expect(() => resolveFrontendComposition({ env: { CONSOLE_COMPOSITION_FILE: `${linked}/../instance.json` } })).toThrow(/符号链接/);
    expect(() => resolveFrontendComposition({ env: { CONSOLE_INSTANCE_PROFILE: 'public' } })).toThrow(/CONSOLE_COMPOSITION_FILE/);
  });

  test('rejects CONSOLE_INSTANCE_PROFILE mismatch, symlinked composition/root, and invalid composition file', async () => {
    const { base, root: extRoot } = await fixture([manifest('ext_alpha')], {}, { dirName: 'custom_ext_mods' });
    const compFile = path.join(base, 'composition.json');
    await writeFile(compFile, JSON.stringify(compositionFilePayload('./custom_ext_mods', 'private')));

    expect(() =>
      resolveFrontendComposition({
        env: {
          CONSOLE_COMPOSITION_FILE: compFile,
          CONSOLE_INSTANCE_PROFILE: 'public',
        },
      }),
    ).toThrow(/不一致/);

    expect(() =>
      resolveFrontendComposition({
        env: { CONSOLE_INSTANCE_PROFILE: 'staging' },
      }),
    ).toThrow(/CONSOLE_INSTANCE_PROFILE 取值无效/);

    const symlinkComp = path.join(base, 'symlink-composition.json');
    await symlink(compFile, symlinkComp);
    expect(() =>
      resolveFrontendComposition({
        env: { CONSOLE_COMPOSITION_FILE: symlinkComp },
      }),
    ).toThrow(/不能是符号链接/);

    const symlinkRoot = path.join(base, 'symlink-plugins-root');
    await symlink(extRoot, symlinkRoot);
    expect(() =>
      resolveFrontendComposition({
        env: { CONSOLE_PLUGIN_ROOT: symlinkRoot },
      }),
    ).toThrow(/不能是符号链接/);
  });

  test('derives Tailwind content, Vitest include, and temporary tsconfig from custom non-plugins root without scanning ../../plugins', async () => {
    const { base, root: extRoot } = await fixture(
      [manifest('ext_mod')],
      {
        ext_mod: 'export default function ExtMod() { return <div className="min-w-[439px]">ext</div>; }',
      },
      { dirName: 'external_bundle_dir' },
    );
    const env = { CONSOLE_PLUGIN_ROOT: extRoot };

    const twContent = resolveTailwindContent({ env });
    expect(twContent.some((item) => item.includes('external_bundle_dir/ext_mod/frontend'))).toBe(true);
    expect(twContent.some((item) => item.includes('../../plugins'))).toBe(false);

    const cssResult = await postcss([
      tailwindcss({
        content: twContent.filter((item) => item.includes('external_bundle_dir')),
        plugins: [tailwindcssAnimate],
      }),
    ]).process('@tailwind utilities;', { from: undefined });
    expect(cssResult.css).toContain('min-w-\\[439px\\]');

    const vitestInclude = resolveVitestInclude({ env });
    expect(vitestInclude).toHaveLength(2);
    expect(vitestInclude[1]).toContain('external_bundle_dir/ext_mod/frontend/**/*.{test,spec}.{ts,tsx}');
    expect(vitestInclude.some((item) => item.includes('../../plugins'))).toBe(false);

    const outTsconfig = path.join(base, 'tsconfig.composition.json');
    const prepared = await prepareFrontendComposition({ env, outPath: outTsconfig, ts });
    const rawTsconfig = JSON.parse(await readFile(prepared.tsconfigPath, 'utf8'));
    const pluginPaths = rawTsconfig.compilerOptions.paths['@plugins/*'] as string[];
    const includeList = rawTsconfig.include as string[];
    expect(pluginPaths[0]).toBe('./external_bundle_dir/*');
    expect(pluginPaths.some((p) => p.includes('../../plugins'))).toBe(false);
    expect(includeList.some((p) => p.includes('external_bundle_dir/ext_mod/frontend'))).toBe(true);
    expect(includeList.some((p) => p.includes('../../plugins'))).toBe(false);
    // tsconfig 在插件根上层时 paths 仍必须带 ./；否则完整 tsc 报 TS5090。
    const parsed = ts.getParsedCommandLineOfConfigFile(prepared.tsconfigPath, { noEmit: true }, {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic: (diagnostic) => {
        throw new Error(ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'));
      },
    });
    const program = ts.createProgram(parsed!.fileNames, parsed!.options);
    expect(ts.getPreEmitDiagnostics(program).map((diagnostic) => diagnostic.code)).not.toContain(5090);
  }, 15000);

  test('validates CONSOLE_BUILD_IDENTITY_FILE whitelist and rejects roots or invalid digests before emitting', async () => {
    const { base } = await fixture([manifest('sample')], {});
    const digestA = 'a'.repeat(64);
    const digestB = 'b'.repeat(64);
    const digestC = 'c'.repeat(64);
    const validIdentity = {
      schema_version: 1,
      kernel: { version: '1.0.0', digest: digestA },
      plugins: [{ id: 'sample', version: '1', digest: digestB }],
      composition_digest: digestC,
    };
    const identityPath = path.join(base, 'identity.json');
    await writeFile(identityPath, JSON.stringify(validIdentity));

    expect(readBuildIdentityFile({ env: {} })).toBeNull();
    expect(() => readBuildIdentityFile({ env: { CONSOLE_BUILD_IDENTITY_FILE: './relative.json' } })).toThrow(/绝对路径/);

    const dirtyIdentityPath = path.join(base, 'dirty-identity.json');
    await writeFile(
      dirtyIdentityPath,
      JSON.stringify({
        ...validIdentity,
        roots: { config: '/opt/secret/config', plugins: '/opt/secret/plugins' },
      }),
    );
    expect(() => readBuildIdentityFile({ env: { CONSOLE_BUILD_IDENTITY_FILE: dirtyIdentityPath } })).toThrow(/组合构建身份字段无效/);

    const duplicateIdentityPath = path.join(base, 'duplicate-identity.json');
    const digest = 'a'.repeat(64);
    await writeFile(
      duplicateIdentityPath,
      `{"schema_version":1,"kernel":{"version":"1","version" : "2","digest":"${digest}"},"plugins":[],"composition_digest":"${digest}"}`,
    );
    expect(() => readBuildIdentityFile({ env: { CONSOLE_BUILD_IDENTITY_FILE: duplicateIdentityPath } })).toThrow(/含重复键：version/);

    const identityLink = path.join(base, 'identity-linked');
    await symlink(path.join(base, 'config'), identityLink);
    expect(() => readBuildIdentityFile({
      env: { CONSOLE_BUILD_IDENTITY_FILE: `${identityLink}/../identity.json` },
    })).toThrow(/符号链接/);
    await rm(identityLink);

    const safeIdentity = readBuildIdentityFile({ env: { CONSOLE_BUILD_IDENTITY_FILE: identityPath } });
    expect(safeIdentity).toEqual(validIdentity);

    process.env.CONSOLE_PLUGIN_ROOT = base;
    process.env.CONSOLE_BUILD_IDENTITY_FILE = identityPath;
    const outDir = path.join(base, 'dist-out');
    await build({
      root: DEFAULT_FRONTEND_ROOT,
      configFile: VITE_CONFIG_PATH,
      logLevel: 'silent',
      build: {
        outDir,
        emptyOutDir: true,
        rollupOptions: {
          input: path.join(base, 'sample/frontend/index.tsx'),
        },
      },
    });
    const emitted = JSON.parse(await readFile(path.join(outDir, 'build-identity.json'), 'utf8'));
    expect(emitted).toEqual(validIdentity);
    expect(emitted).not.toHaveProperty('roots');
  });

  test('prepare_frontend_composition.mjs CLI generates temporary tsconfig and rejects invalid composition', async () => {
    const { base, root: extRoot } = await fixture([manifest('cli_mod')], {}, { dirName: 'cli_custom_plugins' });
    const outPath = path.join(base, 'cli.tsconfig.json');
    const { stdout } = await execFileAsync(process.execPath, [PREPARE_SCRIPT_PATH, '--out', outPath, '--json'], {
      cwd: DEFAULT_FRONTEND_ROOT,
      env: { ...process.env, CONSOLE_PLUGIN_ROOT: extRoot },
    });
    const parsed = JSON.parse(stdout.trim());
    expect(parsed).toMatchObject({
      ok: true,
      profile: 'private',
      source: 'env',
      pluginsRoot: extRoot,
      declaredPlugins: ['cli_mod'],
    });

    await rm(path.join(extRoot, 'composition.json'));
    await expect(
      execFileAsync(process.execPath, [PREPARE_SCRIPT_PATH, '--out', outPath], {
        cwd: DEFAULT_FRONTEND_ROOT,
        env: { ...process.env, CONSOLE_PLUGIN_ROOT: extRoot },
      }),
    ).rejects.toThrow(/插件组合清单/);
  });

  test('Vite dev server loads registry & contract from external non-plugins root and enforces dev composition gate', async () => {
    const baseDefaults = (resolveFrontendComposition({ env: {} }).contract.defaults as Record<string, unknown>) ?? {};
    const { base, root: extRoot } = await fixture(
      [manifest('ext_only')],
      {
        ext_only: 'export default function ExtOnlyPage() { return null; }',
      },
      {
        dirName: 'isolated_addons_tree',
        contractPatch: {
          defaults: {
            ...baseDefaults,
            order: 37,
          },
        },
      },
    );
    const compFile = path.join(base, 'composition.json');
    await writeFile(compFile, JSON.stringify(compositionFilePayload('./isolated_addons_tree', 'private')));

    process.env.CONSOLE_COMPOSITION_FILE = compFile;
    const server = await createServer({
      root: DEFAULT_FRONTEND_ROOT,
      configFile: VITE_CONFIG_PATH,
      logLevel: 'silent',
      server: { middlewareMode: true },
    });
    activeServers.push(server);

    const aliasEntries = server.config.resolve.alias;
    const aliasFinds = Array.isArray(aliasEntries)
      ? aliasEntries.map((entry) => String(entry.find))
      : Object.keys(aliasEntries);
    expect(aliasFinds).not.toContain('@theme');

    const registryModule = await server.ssrLoadModule('/src/lib/plugins/registry.ts');
    expect(registryModule.plugins.map((p: { id: string }) => p.id)).toEqual(['ext_only']);
    expect(registryModule.plugins[0].order).toBe(37);
    await server.close();
    activeServers.pop();

    // Dev gate must reject a symlinked directory without manifest.json inside pluginsRoot
    const emptyTarget = path.join(base, 'empty_dir');
    await mkdir(emptyTarget, { recursive: true });
    await symlink(emptyTarget, path.join(extRoot, 'unlisted_symlink'));
    await expect(
      createServer({
        root: DEFAULT_FRONTEND_ROOT,
        configFile: VITE_CONFIG_PATH,
        logLevel: 'silent',
        server: { middlewareMode: true },
      }),
    ).rejects.toThrow(/不能是符号链接/);

    // Dev gate must reject missing composition.json
    await rm(path.join(extRoot, 'unlisted_symlink'));
    await rm(path.join(extRoot, 'composition.json'));
    await expect(
      createServer({
        root: DEFAULT_FRONTEND_ROOT,
        configFile: VITE_CONFIG_PATH,
        logLevel: 'silent',
        server: { middlewareMode: true },
      }),
    ).rejects.toThrow(/插件组合清单/);
  });
});
