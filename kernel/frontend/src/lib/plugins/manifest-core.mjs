const ID_PATTERN = /^[a-z][a-z0-9_]{0,47}$/;
const OWNER_KEYS = ['tables', 'settings', 'files', 'tasks'];

function fail(message) {
  throw new Error(message);
}

function matches(path, prefix) {
  return path === prefix || path.startsWith(`${prefix.replace(/\/+$/, '')}/`);
}

function requiredString(value, field, id) {
  if (typeof value !== 'string' || value.trim().length === 0) fail(`插件 ${id} 的 ${field} 必须是非空字符串`);
  return value;
}

function stringArray(value, field, id) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || item.trim().length === 0)) {
    fail(`插件 ${id} 的 ${field} 必须是非空字符串数组`);
  }
  return value;
}

export function normalizePluginManifest(value, directoryId, contract) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`插件目录 ${directoryId} 的清单无效`);
  const raw = value;
  const id = requiredString(raw.id, 'id', directoryId);
  if (!ID_PATTERN.test(id) || id !== directoryId) fail(`插件目录 ${directoryId} 与清单 ID 不匹配`);
  if (!Number.isInteger(raw.api_version) || raw.api_version !== contract.api_version) fail(`插件 ${id} 协议不兼容`);
  const name = requiredString(raw.name, 'name', id);
  const description = requiredString(raw.description, 'description', id);
  const version = requiredString(raw.version, 'version', id);
  const routes = stringArray(raw.routes, 'routes', id);
  const api_prefixes = stringArray(raw.api_prefixes, 'api_prefixes', id);
  const order = raw.order === undefined ? contract.defaults.order : raw.order;
  if (!Number.isInteger(order)) fail(`插件 ${id} 的 order 必须是整数`);
  const client_mode = raw.client_mode === undefined ? contract.defaults.client_mode : raw.client_mode;
  if (client_mode !== 'authority' && client_mode !== 'shared') fail(`插件 ${id} 的 client_mode 无效`);
  const persistent = raw.persistent === undefined ? contract.defaults.persistent : raw.persistent;
  const sidebar = raw.sidebar === undefined ? contract.defaults.sidebar : raw.sidebar;
  const immersive = raw.immersive === undefined ? contract.defaults.immersive : raw.immersive;
  const navigation = raw.navigation === undefined ? (contract.defaults.navigation ?? false) : raw.navigation;
  for (const [key, flag] of Object.entries({ persistent, sidebar, immersive, navigation })) {
    if (typeof flag !== 'boolean') fail(`插件 ${id} 的 ${key} 必须为布尔值`);
  }
  let icon;
  if (raw.icon !== undefined) icon = requiredString(raw.icon, 'icon', id);
  let settings;
  if (raw.settings !== undefined) {
    if (!raw.settings || typeof raw.settings !== 'object' || Array.isArray(raw.settings)) fail(`插件 ${id} 的 settings 无效`);
    settings = {
      id: requiredString(raw.settings.id, 'settings.id', id),
      label: requiredString(raw.settings.label, 'settings.label', id),
    };
    if (!ID_PATTERN.test(settings.id) || contract.reserved_settings.includes(settings.id)) fail(`插件 ${id} 的设置页签无效或与框架冲突`);
  }
  let owns;
  if (raw.owns === undefined) fail(`插件 ${id} 必须声明 owns`);
  if (raw.owns !== undefined) {
    if (!raw.owns || typeof raw.owns !== 'object' || Array.isArray(raw.owns) || Object.keys(raw.owns).length !== OWNER_KEYS.length || OWNER_KEYS.some((key) => !(key in raw.owns))) fail(`插件 ${id} 的 owns 无效`);
    owns = Object.fromEntries(OWNER_KEYS.map((key) => [key, stringArray(raw.owns[key], `owns.${key}`, id)]));
    for (const [key, values] of Object.entries(owns)) if (new Set(values).size !== values.length) fail(`插件 ${id} 的 owns.${key} 不能重复`);
  }
  const reservedPaths = [...contract.reserved_routes, ...contract.reserved_api];
  for (const path of [...routes, ...api_prefixes]) {
    if (!path.startsWith('/') || path === '/' || path.includes('\\')) fail(`插件 ${id} 的路径 ${path} 无效`);
    const prefix = path.split('/:')[0].replace(/\/+$/, '') || '/';
    if (reservedPaths.some((reserved) => matches(prefix, reserved) || matches(reserved, prefix))) fail(`插件 ${id} 路径占用框架入口：${path}`);
  }
  return {
    id, name, description, version, api_version: contract.api_version, order, routes, api_prefixes,
    client_mode, persistent, sidebar, immersive, navigation,
    ...(icon === undefined ? {} : { icon }),
    ...(settings ? { settings } : {}),
    ...(owns ? { owns } : {}),
  };
}

export function assertNoPluginPathConflicts(manifests) {
  const ids = new Set();
  const paths = [];
  const settings = new Map();
  const owners = new Map();
  for (const manifest of manifests) {
    if (ids.has(manifest.id)) fail(`插件 ID 重复：${manifest.id}`);
    ids.add(manifest.id);
    for (const path of [...manifest.routes, ...manifest.api_prefixes]) {
      const prefix = path.split('/:')[0].replace(/\/+$/, '') || '/';
      const prior = paths.find(([known, owner]) => owner !== manifest.id && (matches(prefix, known) || matches(known, prefix)));
      if (prior) fail(`插件 ${manifest.id} 与 ${prior[1]} 的路径冲突：${path}`);
      paths.push([prefix, manifest.id]);
    }
    if (manifest.settings) {
      const prior = settings.get(manifest.settings.id);
      if (prior) fail(`插件 ${manifest.id} 与 ${prior} 的设置页签冲突：${manifest.settings.id}`);
      settings.set(manifest.settings.id, manifest.id);
    }
    for (const [kind, values] of Object.entries(manifest.owns ?? {})) {
      for (const value of values) {
        const key = `${kind}\0${value}`;
        const prior = owners.get(key);
        if (prior) fail(`插件 ${manifest.id} 的 ${kind}/${value} 与 ${prior} 所有权冲突`);
        owners.set(key, manifest.id);
      }
    }
  }
}
