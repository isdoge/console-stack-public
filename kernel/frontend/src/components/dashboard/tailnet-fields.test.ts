import { beforeEach, describe, expect, test, vi } from 'vitest';
import {
  TAILNET_FIELDS_STORAGE_KEY,
  readTailnetFields,
  writeTailnetFields,
} from './tailnet-fields';

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('readTailnetFields', () => {
  test('键 absent（没设过）→ null，调用方回退默认全显', () => {
    localStorage.removeItem(TAILNET_FIELDS_STORAGE_KEY);
    expect(readTailnetFields()).toBeNull();
  });

  test('显式空数组是合法状态（最小视图），不与 absent 混淆', () => {
    localStorage.setItem(TAILNET_FIELDS_STORAGE_KEY, '[]');
    expect(readTailnetFields()).toEqual([]);
  });

  test('坏 JSON / 非数组 → null（降级默认全显）', () => {
    localStorage.setItem(TAILNET_FIELDS_STORAGE_KEY, '{不是数组');
    expect(readTailnetFields()).toBeNull();
    localStorage.setItem(TAILNET_FIELDS_STORAGE_KEY, '{"os":true}');
    expect(readTailnetFields()).toBeNull();
  });

  test('非法项丢弃、重复去重、按定义顺序输出', () => {
    localStorage.setItem(
      TAILNET_FIELDS_STORAGE_KEY,
      JSON.stringify(['expiry', 'bogus', 1, 'os', 'expiry'])
    );
    expect(readTailnetFields()).toEqual(['os', 'expiry']);
  });

  test('localStorage 抛异常（隐私模式）→ null，不炸卡', () => {
    const original = localStorage.getItem;
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    try {
      expect(readTailnetFields()).toBeNull();
    } finally {
      localStorage.getItem = original;
    }
  });
});

describe('writeTailnetFields', () => {
  test('写入后读回一致，且按定义顺序落盘', () => {
    localStorage.removeItem(TAILNET_FIELDS_STORAGE_KEY);
    writeTailnetFields(['expiry', 'os']);
    expect(localStorage.getItem(TAILNET_FIELDS_STORAGE_KEY)).toBe(
      JSON.stringify(['os', 'expiry'])
    );
    expect(readTailnetFields()).toEqual(['os', 'expiry']);
  });

  test('空数组显式落盘（用户就要最小视图）', () => {
    writeTailnetFields([]);
    expect(localStorage.getItem(TAILNET_FIELDS_STORAGE_KEY)).toBe('[]');
  });

  test('localStorage 抛异常（隐私模式）时静默降级，不炸卡', () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    try {
      expect(() => writeTailnetFields(['os'])).not.toThrow();
    } finally {
      vi.restoreAllMocks();
    }
  });
});
