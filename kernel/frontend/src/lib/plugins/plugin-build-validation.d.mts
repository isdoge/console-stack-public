export interface FrontendCompositionOptions {
  env?: Record<string, string | undefined>;
  frontendRoot?: string;
  cwd?: string;
  outPath?: string;
  ts?: typeof import('typescript');
}

export interface ResolvedFrontendComposition {
  profile: 'private' | 'public';
  compositionFile: string | null;
  compositionName: string | null;
  pluginsRoot: string;
  contractPath: string;
  compositionPath: string;
  contract: Record<string, unknown>;
  declaredPlugins: string[];
  source: 'env' | 'composition_file' | 'default';
}

export const DEFAULT_FRONTEND_ROOT: string;
export function readCompositionSync(pluginsRoot: string): string[];
export function readComposition(pluginsRoot: string): Promise<string[]>;
export function readContractSync(pluginsRoot: string): Record<string, unknown>;
export function resolveFrontendComposition(options?: FrontendCompositionOptions): ResolvedFrontendComposition;
export function readBuildIdentityFile(options?: Pick<FrontendCompositionOptions, 'env'>): Record<string, unknown> | null;
export function resolveTailwindContent(options?: FrontendCompositionOptions): string[];
export function resolveVitestInclude(options?: FrontendCompositionOptions): string[];
export function resolveEslintPluginFiles(options?: FrontendCompositionOptions): string[];
export function generateCompositionTsconfig(options?: FrontendCompositionOptions): {
  composition: ResolvedFrontendComposition;
  tsconfigPath: string;
  tsconfig: Record<string, unknown>;
};
export function prepareFrontendComposition(options?: FrontendCompositionOptions): Promise<{
  composition: ResolvedFrontendComposition;
  tsconfigPath: string;
  tsconfig: Record<string, unknown>;
}>;
export function validatePluginSources(
  pluginsRoot: string | undefined,
  typescript: typeof import('typescript'),
): Promise<import('./manifest-core.mjs').PluginManifestData[]>;
