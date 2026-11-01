/** 数字版本统一省略 v 前缀；仅供展示，不改变原始标签或升级参数。 */
export function displayVersion(value?: string, fallback = 'N/A'): string {
  const version = value?.trim() || fallback;
  return version.replace(/^v(?=\d)/i, '');
}
