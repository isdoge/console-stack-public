import { lstatSync, readFileSync, writeFileSync } from 'node:fs';
import { lstat, readFile, readdir } from 'node:fs/promises';
import { isIP } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertNoPluginPathConflicts, normalizePluginManifest } from './manifest-core.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const DEFAULT_FRONTEND_ROOT = path.resolve(__dirname, '../../..');

const ID_PATTERN = /^[a-z][a-z0-9_]{0,47}$/;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/;
const PROFILES = new Set(['private', 'public']);
const COMPOSITION_FILE_KEYS = new Set(['schema_version', 'profile', 'roots', 'host', 'alert_machines', 'name']);
const COMPOSITION_ROOT_KEYS = ['config', 'data', 'static', 'backup', 'plugins'];
const HOST_KEYS = new Set([
  'local_machine',
  'home_machines',
  'excluded_machine_ids',
  'home_lan_ip',
  'host_control_key',
  'host_control_target',
]);
const CONTROL_TARGET_PATTERN = /^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}$/;
const IDENTITY_KEYS = new Set(['schema_version', 'kernel', 'plugins', 'composition_digest']);
const KERNEL_IDENTITY_KEYS = new Set(['version', 'digest']);
const PLUGIN_IDENTITY_KEYS = new Set(['id', 'version', 'digest']);

function parseStrictJson(text, label) {
  let document;
  try { document = JSON.parse(text); }
  catch { throw new Error(`${label}不是有效 JSON`); }
  const objectKeys = [];
  let index = 0;
  while (index < text.length) {
    const character = text[index];
    if (character === '"') {
      const start = index++;
      while (index < text.length) {
        if (text[index] === '\\') index += 2;
        else if (text[index++] === '"') break;
      }
      const key = JSON.parse(text.slice(start, index));
      while (/[ \t\r\n]/.test(text[index] ?? '')) index += 1;
      if (text[index] === ':' && objectKeys.length > 0) {
        const keys = objectKeys[objectKeys.length - 1];
        if (keys.has(key)) throw new Error(`${label}含重复键：${key}`);
        keys.add(key);
      }
      continue;
    }
    if (character === '{') objectKeys.push(new Set());
    else if (character === '}') objectKeys.pop();
    index += 1;
  }
  return document;
}

function validateCompositionHost(host) {
  const unknown = Object.keys(host).filter((key) => !HOST_KEYS.has(key));
  if (unknown.length) throw new Error(`实例组合文件 host 含未知字段：${unknown.sort().join(', ')}`);
  const validateIds = (key) => {
    const values = host[key];
    if (values === undefined || values === null) return;
    if (!Array.isArray(values) || values.some((value) => typeof value !== 'string' || !ID_PATTERN.test(value))) {
      throw new Error(`实例组合文件 host.${key} 含无效机器 id`);
    }
    if (new Set(values).size !== values.length) throw new Error(`实例组合文件 host.${key} 不能重复`);
  };
  for (const key of ['home_machines', 'excluded_machine_ids']) validateIds(key);
  if (host.local_machine !== undefined && host.local_machine !== null && (typeof host.local_machine !== 'string' || (host.local_machine !== '' && !ID_PATTERN.test(host.local_machine)))) {
    throw new Error('实例组合文件 host.local_machine 必须为空或小写机器 id');
  }
  for (const key of ['host_control_key', 'host_control_target']) {
    const value = host[key];
    if (value !== undefined && value !== null && (typeof value !== 'string' || /[\r\n\0]/.test(value))) {
      throw new Error(`实例组合文件 host.${key} 必须是无控制字符的字符串`);
    }
  }
  if (host.host_control_target !== undefined && host.host_control_target !== null && host.host_control_target !== '' && !CONTROL_TARGET_PATTERN.test(host.host_control_target)) {
    throw new Error('实例组合文件 host.host_control_target 格式无效');
  }
  const ip = host.home_lan_ip;
  if (ip !== undefined && ip !== null && ip !== '' && (typeof ip !== 'string' || isIP(ip) === 0)) {
    throw new Error('实例组合文件 host.home_lan_ip 必须是 IP 地址');
  }
}

function validateCompositionAlerts(alertMachines) {
  for (const [machine, spec] of Object.entries(alertMachines)) {
    if (!ID_PATTERN.test(machine) || !spec || typeof spec !== 'object' || Array.isArray(spec) || Object.keys(spec).length !== 2 || !Object.hasOwn(spec, 'display') || !Object.hasOwn(spec, 'load5_threshold')) {
      throw new Error(`实例组合文件 alert_machines.${machine} 格式无效`);
    }
    if (typeof spec.display !== 'string' || !spec.display.trim() || typeof spec.load5_threshold !== 'number' || !Number.isFinite(spec.load5_threshold) || spec.load5_threshold <= 0 || spec.load5_threshold >= 1e6) {
      throw new Error(`实例组合文件 alert_machines.${machine} load5_threshold 必须是大于 0 且小于 1e6 的数值`);
    }
  }
}
function exportedNames(sourceFile, ts) {
  const names = new Set();
  for (const statement of sourceFile.statements) {
    if (ts.isExportAssignment(statement) && !statement.isExportEquals) names.add('default');
    if (ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement) || ts.isVariableStatement(statement)) {
      const flags = ts.getCombinedModifierFlags(statement);
      if (flags & ts.ModifierFlags.Export) {
        if (flags & ts.ModifierFlags.Default) names.add('default');
        if (ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement)) {
          if (statement.name) names.add(statement.name.text);
        } else {
          for (const declaration of statement.declarationList.declarations) if (ts.isIdentifier(declaration.name)) names.add(declaration.name.text);
        }
      }
    }
    if (ts.isExportDeclaration(statement) && !statement.isTypeOnly && statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      for (const element of statement.exportClause.elements) if (!element.isTypeOnly) names.add(element.name.text);
    }
  }
  return names;
}

function lstatOrNullSync(target) {
  try {
    return lstatSync(target);
  } catch {
    return null;
  }
}

function expandHome(value) {
  return value === '~' ? os.homedir() : value.startsWith('~/') ? path.join(os.homedir(), value.slice(2)) : value;
}

function assertNoParentSymlinkSync(targetPath, baseDir, rawTarget, label) {
  if (baseDir && rawTarget) {
    let cursor = path.isAbsolute(rawTarget) ? path.parse(rawTarget).root : path.resolve(baseDir);
    for (const part of String(rawTarget).split(path.sep)) {
      if (!part || part === '.') continue;
      cursor = path.resolve(cursor, part);
      if (part === '..') continue;
      const partInfo = lstatOrNullSync(cursor);
      if (partInfo?.isSymbolicLink()) {
        throw new Error(`${label} 不能是符号链接：${part}`);
      }
    }
  }
  let current = path.resolve(targetPath);
  while (path.dirname(current) !== '/' && path.dirname(current) !== current) {
    const info = lstatOrNullSync(current);
    if (info?.isSymbolicLink()) {
      throw new Error(`${label} 不能是符号链接：${path.basename(current)}`);
    }
    current = path.dirname(current);
  }
}

function assertRegularDirectorySync(dirPath, baseDir, rawTarget, label) {
  assertNoParentSymlinkSync(dirPath, baseDir, rawTarget, label);
  const info = lstatOrNullSync(dirPath);
  if (info?.isSymbolicLink()) throw new Error(`${label} 不能是符号链接`);
  if (!info?.isDirectory()) throw new Error(`${label} 缺失或不是普通目录`);
}

function assertRegularFileSync(filePath, label, baseDir = null, rawTarget = null) {
  const info = lstatOrNullSync(filePath);
  if (info?.isSymbolicLink()) throw new Error(`${label} 缺失或不是普通文件（不能是符号链接）`);
  if (!info?.isFile()) throw new Error(`${label} 缺失或不是普通文件`);
  assertNoParentSymlinkSync(path.dirname(filePath), baseDir, rawTarget ? path.dirname(rawTarget) : null, label);
}

async function assertRegularFile(file, label) {
  const info = await lstat(file).catch(() => null);
  if (!info?.isFile() || info.isSymbolicLink()) throw new Error(`${label} 缺失或不是普通文件`);
}

function validateDeclaredPluginList(document) {
  if (!document || typeof document !== 'object' || Array.isArray(document) || Object.keys(document).length !== 1 || !Array.isArray(document.plugins)) {
    throw new Error('插件组合清单顶层必须只含 plugins 数组');
  }
  // 空数组是合法的显式「零插件」组合；缺失清单才是错误，两者不能混为一谈。
  const ids = document.plugins;
  const seen = new Set();
  for (const id of ids) {
    if (typeof id !== 'string' || !ID_PATTERN.test(id)) throw new Error(`插件组合清单含无效插件 id：${String(id)}`);
    if (seen.has(id)) throw new Error(`插件组合清单插件 id 重复：${id}`);
    seen.add(id);
  }
  return ids;
}

export function readCompositionSync(pluginsRoot) {
  const resolvedRoot = path.resolve(pluginsRoot);
  assertRegularDirectorySync(resolvedRoot, null, null, '插件根目录');
  const compositionPath = path.join(resolvedRoot, 'composition.json');
  assertRegularFileSync(compositionPath, '插件组合清单');
  let document;
  try { document = parseStrictJson(readFileSync(compositionPath, 'utf8'), '插件组合清单'); }
  catch (error) { throw error instanceof Error ? error : new Error('插件组合清单不是有效 JSON'); }
  return validateDeclaredPluginList(document);
}

/**
 * 读取组合来源清单（`composition.json`）。后端启动、本构建校验和离线检查器
 * 都读同一份文件，禁止各自扫描目录得到不同集合（B7-1）。
 */
export async function readComposition(pluginsRoot) {
  return readCompositionSync(pluginsRoot);
}

export function readContractSync(pluginsRoot) {
  const resolvedRoot = path.resolve(pluginsRoot);
  assertRegularDirectorySync(resolvedRoot, null, null, '插件根目录');
  const contractPath = path.join(resolvedRoot, 'contract.json');
  assertRegularFileSync(contractPath, '插件共享契约');
  let contract;
  try { contract = JSON.parse(readFileSync(contractPath, 'utf8')); }
  catch { throw new Error('插件共享契约不是有效 JSON'); }
  if (!contract || typeof contract !== 'object' || Array.isArray(contract) || !Number.isInteger(contract.api_version)) {
    throw new Error('插件共享契约格式无效');
  }
  return contract;
}

/**
 * 解析前端组合输入（与后端 composition 协议一致）：
 * - `CONSOLE_COMPOSITION_FILE`: JSON { schema_version: 1, profile: 'private'|'public', roots: { config, data, static, backup, plugins }, host, alert_machines, name? }
 * - `CONSOLE_PLUGIN_ROOT`: 显式 env 覆盖插件根（必须为含 contract.json 与 composition.json 的普通目录，禁止软链）
 * - `CONSOLE_INSTANCE_PROFILE`: 若显式设置且与组合文件 profile 不符则拒绝
 * - 未提供组合文件与插件根时默认 `<frontendRoot>/../../plugins`
 */
export function resolveFrontendComposition(options = {}) {
  const env = options.env ?? process.env;
  const frontendRoot = path.resolve(options.frontendRoot ?? DEFAULT_FRONTEND_ROOT);
  const cwd = path.resolve(options.cwd ?? process.cwd());

  const rawProfile = env.CONSOLE_INSTANCE_PROFILE;
  const envProfile = rawProfile === undefined ? '' : String(rawProfile).trim().toLowerCase();
  if (envProfile !== '' && !PROFILES.has(envProfile)) {
    throw new Error(`CONSOLE_INSTANCE_PROFILE 取值无效：${String(rawProfile).trim()}，只允许 private / public`);
  }

  const rawCompositionFile = (env.CONSOLE_COMPOSITION_FILE ?? '').trim();
  let compositionFile = null;
  let fileProfile = null;
  let filePluginsRoot = null;
  let rawFilePluginsRoot = null;
  let compositionDir = null;
  let compositionName = null;

  if (rawCompositionFile !== '') {
    const expandedCompositionFile = expandHome(rawCompositionFile);
    compositionFile = path.resolve(cwd, expandedCompositionFile);
    assertRegularFileSync(compositionFile, '实例组合文件', cwd, expandedCompositionFile);
    let doc;
    try { doc = parseStrictJson(readFileSync(compositionFile, 'utf8'), '实例组合文件'); }
    catch (error) { throw error instanceof Error ? error : new Error('实例组合文件不是有效 JSON'); }
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) {
      throw new Error('实例组合文件顶层必须是对象');
    }
    for (const key of Object.keys(doc)) {
      if (!COMPOSITION_FILE_KEYS.has(key)) {
        throw new Error(`实例组合文件含未知字段：${key}`);
      }
    }
    if (doc.schema_version !== 1) {
      throw new Error(`实例组合文件 schema_version 必须为 1，实际为 ${String(doc.schema_version)}`);
    }
    if (typeof doc.profile !== 'string' || !PROFILES.has(doc.profile)) {
      throw new Error(`实例组合文件 profile 无效：${String(doc.profile)}`);
    }
    fileProfile = doc.profile;
    if (envProfile !== '' && envProfile !== fileProfile) {
      throw new Error(`CONSOLE_INSTANCE_PROFILE (${envProfile}) 与实例组合文件 profile (${fileProfile}) 不一致`);
    }
    if (doc.name !== undefined) {
      if (typeof doc.name !== 'string' || doc.name.trim().length === 0) {
        throw new Error('实例组合文件 name 必须是非空字符串');
      }
      compositionName = doc.name;
    }
    if (doc.host === null || typeof doc.host !== 'object' || Array.isArray(doc.host)) {
      throw new Error('实例组合文件 host 必须是对象');
    }
    if (!doc.alert_machines || typeof doc.alert_machines !== 'object' || Array.isArray(doc.alert_machines)) {
      throw new Error('实例组合文件 alert_machines 必须是对象');
    }
    validateCompositionHost(doc.host);
    validateCompositionAlerts(doc.alert_machines);
    if (!doc.roots || typeof doc.roots !== 'object' || Array.isArray(doc.roots)) {
      throw new Error('实例组合文件 roots 必须是对象');
    }
    const rootKeys = Object.keys(doc.roots);
    if (rootKeys.length !== COMPOSITION_ROOT_KEYS.length || COMPOSITION_ROOT_KEYS.some((key) => !Object.hasOwn(doc.roots, key))) {
      throw new Error('实例组合文件 roots 必须且只能包含 config/data/static/backup/plugins');
    }
    for (const key of COMPOSITION_ROOT_KEYS) {
      const value = doc.roots[key];
      if (typeof value !== 'string' || value.trim().length === 0 || value.includes('\0')) {
        throw new Error(`实例组合文件 roots.${key} 必须是非空字符串`);
      }
    }
    compositionDir = path.dirname(compositionFile);
    for (const key of COMPOSITION_ROOT_KEYS) {
      const value = expandHome(doc.roots[key]);
      const root = path.resolve(compositionDir, value);
      assertNoParentSymlinkSync(root, compositionDir, value, `组合文件 roots.${key}`);
      if (key === 'config' || key === 'plugins') {
        assertRegularDirectorySync(root, compositionDir, value, `组合文件 roots.${key}`);
      }
    }
    rawFilePluginsRoot = expandHome(doc.roots.plugins);
    filePluginsRoot = path.resolve(compositionDir, rawFilePluginsRoot);
  }
  if (envProfile === 'public' && compositionFile === null) {
    throw new Error('公共实例必须提供 CONSOLE_COMPOSITION_FILE');
  }

  const rawPluginRoot = expandHome((env.CONSOLE_PLUGIN_ROOT ?? '').trim());
  let pluginsRoot;
  let source;
  if (rawPluginRoot !== '') {
    pluginsRoot = path.resolve(cwd, rawPluginRoot);
    source = 'env';
    assertRegularDirectorySync(pluginsRoot, cwd, rawPluginRoot, 'CONSOLE_PLUGIN_ROOT 插件根目录');
  } else if (filePluginsRoot !== null) {
    pluginsRoot = filePluginsRoot;
    source = 'composition_file';
    assertRegularDirectorySync(pluginsRoot, compositionDir, rawFilePluginsRoot, '组合文件 plugins 根目录');
  } else {
    pluginsRoot = path.resolve(frontendRoot, '../../plugins');
    source = 'default';
    assertRegularDirectorySync(pluginsRoot, frontendRoot, '../../plugins', '默认插件根目录');
  }

  const contract = readContractSync(pluginsRoot);
  const declaredPlugins = readCompositionSync(pluginsRoot);
  const profile = fileProfile ?? (envProfile || 'private');

  return {
    profile,
    compositionFile,
    compositionName,
    pluginsRoot,
    contractPath: path.join(pluginsRoot, 'contract.json'),
    compositionPath: path.join(pluginsRoot, 'composition.json'),
    contract,
    declaredPlugins,
    source,
  };
}

/**
 * 读取主会话传入的构建身份文件（`CONSOLE_BUILD_IDENTITY_FILE`）。
 * 与后端 `composition_identity.validate` 同口径：仅接受白名单字段
 * `{schema_version, kernel, plugins, composition_digest}`，拒绝含 `roots` 或未知字段。
 */
export function readBuildIdentityFile(options = {}) {
  const env = options.env ?? process.env;
  const rawPath = (env.CONSOLE_BUILD_IDENTITY_FILE ?? '').trim();
  if (!rawPath) return null;
  if (!path.isAbsolute(rawPath)) {
    throw new Error('CONSOLE_BUILD_IDENTITY_FILE 必须是绝对路径');
  }
  assertRegularFileSync(rawPath, '组合构建身份', process.cwd(), rawPath);
  let document;
  try { document = parseStrictJson(readFileSync(rawPath, 'utf8'), '组合构建身份'); }
  catch (error) { throw error instanceof Error ? error : new Error('组合构建身份不可读取或格式错误'); }
  if (
    !document ||
    typeof document !== 'object' ||
    Array.isArray(document) ||
    Object.keys(document).length !== IDENTITY_KEYS.size ||
    Object.keys(document).some((key) => !IDENTITY_KEYS.has(key))
  ) {
    throw new Error('组合构建身份字段无效');
  }
  if (!Number.isInteger(document.schema_version) || document.schema_version !== 1) {
    throw new Error('组合构建身份版本不支持');
  }
  const kernel = document.kernel;
  if (
    !kernel ||
    typeof kernel !== 'object' ||
    Array.isArray(kernel) ||
    Object.keys(kernel).length !== KERNEL_IDENTITY_KEYS.size ||
    Object.keys(kernel).some((key) => !KERNEL_IDENTITY_KEYS.has(key))
  ) {
    throw new Error('内核构建身份无效');
  }
  if (typeof kernel.version !== 'string' || !VERSION_PATTERN.test(kernel.version)) {
    throw new Error('内核版本无效');
  }
  if (
    typeof kernel.digest !== 'string' ||
    !DIGEST_PATTERN.test(kernel.digest) ||
    typeof document.composition_digest !== 'string' ||
    !DIGEST_PATTERN.test(document.composition_digest)
  ) {
    throw new Error('组合摘要无效');
  }
  if (!Array.isArray(document.plugins)) {
    throw new Error('组合插件身份必须是数组');
  }
  const seen = new Set();
  for (const plugin of document.plugins) {
    if (
      !plugin ||
      typeof plugin !== 'object' ||
      Array.isArray(plugin) ||
      Object.keys(plugin).length !== PLUGIN_IDENTITY_KEYS.size ||
      Object.keys(plugin).some((key) => !PLUGIN_IDENTITY_KEYS.has(key))
    ) {
      throw new Error('插件构建身份字段无效');
    }
    if (typeof plugin.id !== 'string' || !ID_PATTERN.test(plugin.id) || seen.has(plugin.id)) {
      throw new Error('插件构建身份重复或无效');
    }
    if (typeof plugin.version !== 'string' || !VERSION_PATTERN.test(plugin.version)) {
      throw new Error('插件版本无效');
    }
    if (typeof plugin.digest !== 'string' || !DIGEST_PATTERN.test(plugin.digest)) {
      throw new Error('插件摘要无效');
    }
    seen.add(plugin.id);
  }
  return document;
}

export function resolveTailwindContent(options = {}) {
  const frontendRoot = path.resolve(options.frontendRoot ?? DEFAULT_FRONTEND_ROOT);
  const { pluginsRoot, declaredPlugins } = resolveFrontendComposition({ ...options, frontendRoot });
  return [
    './index.html',
    './src/**/*.{js,jsx,ts,tsx}',
    ...declaredPlugins.map((id) =>
      path.join(pluginsRoot, id, 'frontend/**/*.{js,jsx,ts,tsx}').split(path.sep).join('/'),
    ),
  ];
}

export function resolveVitestInclude(options = {}) {
  const frontendRoot = path.resolve(options.frontendRoot ?? DEFAULT_FRONTEND_ROOT);
  const { pluginsRoot, declaredPlugins } = resolveFrontendComposition({ ...options, frontendRoot });
  const relPluginsRoot = path.relative(frontendRoot, pluginsRoot).split(path.sep).join('/') || '.';
  return [
    'src/**/*.{test,spec}.{ts,tsx}',
    ...declaredPlugins.map((id) => `${relPluginsRoot}/${id}/frontend/**/*.{test,spec}.{ts,tsx}`),
  ];
}

export function resolveEslintPluginFiles(options = {}) {
  const frontendRoot = path.resolve(options.frontendRoot ?? DEFAULT_FRONTEND_ROOT);
  const { pluginsRoot, declaredPlugins } = resolveFrontendComposition({ ...options, frontendRoot });
  const relPluginsRoot = path.relative(frontendRoot, pluginsRoot).split(path.sep).join('/') || '.';
  return declaredPlugins.map((id) => `${relPluginsRoot}/${id}/frontend/**/*.{ts,tsx}`);
}

export function generateCompositionTsconfig(options = {}) {
  const frontendRoot = path.resolve(options.frontendRoot ?? DEFAULT_FRONTEND_ROOT);
  const cwd = path.resolve(options.cwd ?? process.cwd());
  const outPath = path.resolve(cwd, options.outPath ?? path.join(frontendRoot, '.tsconfig.composition.json'));
  const configDir = path.dirname(outPath);
  const composition = resolveFrontendComposition({ ...options, frontendRoot, cwd });
  const baseTsconfig = JSON.parse(readFileSync(path.join(frontendRoot, 'tsconfig.json'), 'utf8'));

  const toRel = (target) => {
    const rel = path.relative(configDir, target).split(path.sep).join('/');
    if (!rel) return '.';
    return rel.startsWith('.') ? rel : `./${rel}`;
  };

  const relSrc = configDir === frontendRoot ? 'src' : toRel(path.join(frontendRoot, 'src'));
  const relSrcPath = toRel(path.join(frontendRoot, 'src'));
  const relPlugins = toRel(composition.pluginsRoot);
  const relNodeModules = toRel(path.join(frontendRoot, 'node_modules'));
  const relTsconfigNode = toRel(path.join(frontendRoot, 'tsconfig.node.json'));

  const tsconfig = {
    ...baseTsconfig,
    compilerOptions: {
      ...baseTsconfig.compilerOptions,
      types: configDir === frontendRoot
        ? baseTsconfig.compilerOptions.types
        : [`${relNodeModules}/vitest/globals`, `${relNodeModules}/vite/client`],
      paths: {
        '@/*': [`${relSrcPath}/*`],
        '@plugins/*': [`${relPlugins}/*`],
        '*': [`${relNodeModules}/@types/*`, `${relNodeModules}/*`],
      },
    },
    include: [
      relSrc,
      `${relSrc}/types`,
      ...composition.declaredPlugins.map((id) => `${relPlugins}/${id}/frontend`),
    ],
    references: [
      {
        path: relTsconfigNode,
      },
    ],
  };

  return { composition, tsconfigPath: outPath, tsconfig };
}

export async function prepareFrontendComposition(options = {}) {
  const result = generateCompositionTsconfig(options);
  if (options.ts) {
    await validatePluginSources(result.composition.pluginsRoot, options.ts);
  }
  writeFileSync(result.tsconfigPath, `${JSON.stringify(result.tsconfig, null, 2)}\n`, 'utf8');
  return result;
}

export async function validatePluginSources(pluginsRoot, ts) {
  const resolvedRoot = path.resolve(pluginsRoot ?? resolveFrontendComposition().pluginsRoot);
  const contract = readContractSync(resolvedRoot);
  const declared = await readComposition(resolvedRoot);
  const children = (await readdir(resolvedRoot, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name));
  const manifests = [];

  for (const entry of children) {
    // 任意符号链接先判：不论是否带 manifest.json 或指向不存在目标，一律拒绝。
    if (entry.isSymbolicLink()) {
      throw new Error(`插件目录 ${entry.name} 不能是符号链接`);
    }
    if (!entry.isDirectory()) continue;
    const directory = path.join(resolvedRoot, entry.name);
    const manifestPath = path.join(directory, 'manifest.json');
    const manifestInfo = await lstat(manifestPath).catch(() => null);
    if (!manifestInfo) continue;
    if (manifestInfo.isSymbolicLink() || !manifestInfo.isFile()) {
      throw new Error(`插件 ${entry.name} manifest.json 缺失或不是普通文件`);
    }
    if (!declared.includes(entry.name)) {
      throw new Error(`插件目录 ${entry.name} 未在组合清单登记`);
    }
    let raw;
    try { raw = JSON.parse(await readFile(manifestPath, 'utf8')); }
    catch { throw new Error(`插件 ${entry.name} manifest.json 不是有效 JSON`); }
    const manifest = normalizePluginManifest(raw, entry.name, contract);
    const frontendDir = path.join(directory, 'frontend');
    const frontendInfo = await lstat(frontendDir).catch(() => null);
    if (frontendInfo?.isSymbolicLink() || !frontendInfo?.isDirectory()) {
      throw new Error(`插件 ${entry.name} frontend 目录缺失或不是普通目录`);
    }
    const entryPath = path.join(frontendDir, 'index.tsx');
    await assertRegularFile(entryPath, `插件 ${entry.name} 前端入口`);
    const source = await readFile(entryPath, 'utf8');
    const sourceFile = ts.createSourceFile(entryPath, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    if (sourceFile.parseDiagnostics.length) throw new Error(`插件 ${entry.name} 前端入口语法无效`);
    const exports = exportedNames(sourceFile, ts);
    if (!exports.has('default')) throw new Error(`插件 ${entry.name} 前端入口缺少 default export`);
    if (manifest.sidebar && !exports.has('Sidebar')) throw new Error(`插件 ${entry.name} 声明了 Sidebar，但入口未导出`);
    if (manifest.settings && !exports.has('Settings')) throw new Error(`插件 ${entry.name} 声明了 Settings，但入口未导出`);
    if (manifest.navigation && !exports.has('loadNavigation')) throw new Error(`插件 ${entry.name} 声明了 loadNavigation，但入口未导出`);
    manifests.push(manifest);
  }
  const onDisk = manifests.map((manifest) => manifest.id);
  const missing = declared.filter((id) => !onDisk.includes(id));
  if (missing.length) throw new Error(`组合清单声明了不存在的插件：${missing.join(', ')}`);
  assertNoPluginPathConflicts(manifests);
  return manifests;
}
