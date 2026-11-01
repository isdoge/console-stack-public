import { describe, expect, test } from 'vitest';
import { displayTime } from './display-time';

describe('displayTime', () => {
  test('按浏览器本地日期分量显示，跨 UTC 日期仍是同一时刻', () => {
    const local = new Date(2026, 8, 26, 0, 15, 30);
    const result = displayTime(local.toISOString());
    expect(result.text).toBe('2026-09-26 00:15:30');
    expect(result.dateTime).toBe(local.toISOString());
    expect(result.title).toContain(local.toISOString());
  });
  test('显式来源 UTC 才解释旧无时区文本，不把日期或未知时区猜成 UTC', () => {
    expect(displayTime('2026-09-25 23:50:00', { assumeUtc: true }).dateTime).toBe('2026-09-25T23:50:00.000Z');
    expect(displayTime('2026-09-25 23:50:00').dateTime).toBeUndefined();
    expect(displayTime('2026-09-25', { assumeUtc: true }).text).toBe('2026-09-25');
    expect(displayTime('bad').text).toBe('bad');
    expect(displayTime().text).toBe('—');
  });
  test('相对时间使用受控时钟，未来时间不误显示刚刚', () => {
    const now = Date.parse('2026-09-25T12:00:00Z');
    expect(displayTime('2026-09-25T11:58:00Z', { now, relative: true }).text).toBe('2 分钟前');
    expect(displayTime('2026-09-25T12:10:00Z', { now, relative: true }).text).not.toBe('刚刚');
    expect(displayTime('2026-09-26T00:00:00+08:00').dateTime).toBe('2026-09-25T16:00:00.000Z');
  });
});
