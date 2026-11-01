import contract from '@plugins/contract.json';
import { assertNoPluginPathConflicts as assertConflicts, normalizePluginManifest as normalize, type PluginManifestData } from './manifest-core.mjs';

export type PluginManifest = PluginManifestData;

export function normalizePluginManifest(value: unknown, directoryId: string): PluginManifest {
  return normalize(value, directoryId, contract);
}

export function assertNoPluginPathConflicts(manifests: PluginManifest[]): void {
  assertConflicts(manifests);
}
