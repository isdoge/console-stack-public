export interface PluginManifestData {
  id: string;
  name: string;
  description: string;
  version: string;
  api_version: number;
  order: number;
  routes: string[];
  api_prefixes: string[];
  client_mode: 'authority' | 'shared';
  persistent: boolean;
  sidebar: boolean;
  immersive: boolean;
  navigation: boolean;
  icon?: string;
  settings?: { id: string; label: string };
  owns?: { tables: string[]; settings: string[]; files: string[]; tasks: string[] };
}

export interface PluginContract {
  api_version: number;
  defaults: { order: number; client_mode: string; persistent: boolean; sidebar: boolean; immersive: boolean; navigation?: boolean };
  reserved_routes: string[];
  reserved_api: string[];
  reserved_settings: string[];
}

export function normalizePluginManifest(value: unknown, directoryId: string, contract: PluginContract): PluginManifestData;
export function assertNoPluginPathConflicts(manifests: PluginManifestData[]): void;
