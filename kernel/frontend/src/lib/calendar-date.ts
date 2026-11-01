/** 日期字段不带时区：用 UTC 容器保存日历分量，避免 UTC 解析和本地午夜/DST 混算。 */
export function calendarDate(year: number, month: number, day: number): Date | null {
  if (!Number.isInteger(year) || year < 1 || year > 9999 ||
      !Number.isInteger(month) || month < 1 || month > 12 ||
      !Number.isInteger(day) || day < 1 || day > 31) return null;
  const date = new Date(0);
  // Date.UTC / new Date 的 0..99 年有 1900 偏移，setUTCFullYear 没有。
  date.setUTCFullYear(year, month - 1, day);
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 ||
      date.getUTCDate() !== day) return null;
  return date;
}

export function parseCalendarDate(value?: string | null): Date | null {
  if (!value) return null;
  const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(value.trim());
  return match ? calendarDate(Number(match[1]), Number(match[2]), Number(match[3])) : null;
}

/** created_at 可为服务端 UTC 时间戳；日期字段请使用 parseCalendarDate。 */
export function parseCreatedAt(value?: string | null): Date | null {
  const dateOnly = parseCalendarDate(value);
  if (dateOnly) return dateOnly;
  if (!value || !parseCalendarDate(value.slice(0, 10)) || !/^[0-9-]{10}[T ]/.test(value)) return null;
  const timestamp = value.trim().replace(' ', 'T');
  const zoned = /(?:Z|[+-]\d{2}:\d{2})$/i.test(timestamp) ? timestamp : `${timestamp}Z`;
  const parsed = new Date(zoned);
  return Number.isFinite(parsed.getTime()) ? parsed : null;
}
