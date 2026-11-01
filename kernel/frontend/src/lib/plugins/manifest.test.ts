import { describe, expect, test } from 'vitest';
import { assertNoPluginPathConflicts, normalizePluginManifest } from './manifest';

const manifest = (id: string, patch: Record<string, unknown> = {}) => normalizePluginManifest({
  id, name: id, description: `${id} description`, version: '1.0', api_version: 1, owns: { tables: [], settings: [], files: [], tasks: [] },
  routes: [`/${id}`], api_prefixes: [`/api/${id}`], ...patch,
}, id);

describe('plugin manifest normalization', () => {
  test('fills stable defaults and sorts missing order after explicit lower orders', () => {
    const defaults = manifest('sample');
    expect(defaults).toMatchObject({ order: 100, client_mode: 'authority', persistent: false, sidebar: false, immersive: false });
    expect([manifest('later'), manifest('first', { order: 1 })].sort((a, b) => a.order - b.order).map((row) => row.id)).toEqual(['first', 'later']);
  });

  test('requires directory identity, protocol, required strings, path arrays and boolean flags', () => {
    expect(() => normalizePluginManifest({ id: 'other' }, 'sample')).toThrow(/目录 sample 与清单 ID/);
    expect(() => manifest('bad', { api_version: 2 })).toThrow(/协议不兼容/);
    expect(() => manifest('bad', { description: ' ' })).toThrow(/description/);
    expect(() => manifest('bad', { routes: ['/bad', 3] })).toThrow(/routes/);
    expect(() => manifest('bad', { sidebar: 'yes' })).toThrow(/sidebar/);
    expect(() => manifest('bad', { navigation: 'yes' })).toThrow(/navigation/);
  });

  test('preserves and validates optional ownership metadata', () => {
    const owns = { tables: ['feed_items'], settings: [], files: ['bookmarks.json'], tasks: [] };
    expect(manifest('sample', { owns }).owns).toEqual(owns);
    expect(() => manifest('sample', { owns: { tables: ['x', 1], settings: [], files: [], tasks: [] } })).toThrow(/owns.tables/);
    expect(() => manifest('sample', { owns: { tables: ['x', 'x'], settings: [], files: [], tasks: [] } })).toThrow(/不能重复/);
  });

  test('rejects framework, duplicate, overlapping API and settings paths', () => {
    expect(() => assertNoPluginPathConflicts([manifest('one'), manifest('two', { routes: ['/one/sub'] })])).toThrow(/路径冲突/);
    expect(() => assertNoPluginPathConflicts([manifest('one'), manifest('two', { api_prefixes: ['/api/one/sub'] })])).toThrow(/路径冲突/);
    expect(() => manifest('one', { routes: ['/settings/custom'] })).toThrow(/框架入口/);
    expect(() => manifest('one', { settings: { id: 'about', label: 'About' } })).toThrow(/框架冲突/);
    expect(() => assertNoPluginPathConflicts([manifest('one', { settings: { id: 'custom', label: 'A' } }), manifest('two', { settings: { id: 'custom', label: 'B' } })])).toThrow(/设置页签冲突/);
  });
});
