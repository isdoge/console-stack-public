/** 时间戳展示；日历日期（到期日/账单日）不进入这里。 */
export interface TimeOptions {
  assumeUtc?: boolean;
  relative?: boolean;
  minute?: boolean;
  now?: number;
}

export function displayTime(value?: string | null, options: TimeOptions = {}) {
  const raw = value?.trim() || '';
  const missing = { text: '—', title: '无时间记录', dateTime: undefined };
  if (!raw) return missing;
  const zone = raw.match(/(Z|[+-]\d{2}:?\d{2})$/i)?.[1];
  const timestamp = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(raw);
  if (!timestamp || (!zone && !options.assumeUtc)) {
    return { text: raw, title: `原始值：${raw}\n未标明时区，保留来源时间`, dateTime: undefined };
  }
  const normalized = raw.replace(' ', 'T') + (zone ? '' : 'Z');
  const date = new Date(normalized);
  if (!Number.isFinite(date.getTime())) {
    return { text: raw, title: `原始值：${raw}\n无法解析时间`, dateTime: undefined };
  }
  const pad = (n: number) => String(n).padStart(2, '0');
  const local = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  let text = options.minute ? local.slice(0, 16) : local;
  const age = (options.now ?? Date.now()) - date.getTime();
  if (options.relative && age >= 0) {
    if (age < 60_000) text = '刚刚';
    else if (age < 3_600_000) text = `${Math.floor(age / 60_000)} 分钟前`;
    else if (age < 86_400_000) text = `${Math.floor(age / 3_600_000)} 小时前`;
    else if (age < 7 * 86_400_000) text = `${Math.floor(age / 86_400_000)} 天前`;
  }
  return {
    text,
    title: `本地：${local} (${Intl.DateTimeFormat().resolvedOptions().timeZone})\n原始值：${raw}\n来源时区：${zone || 'UTC（来源约定）'}`,
    dateTime: date.toISOString(),
  };
}
