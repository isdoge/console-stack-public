import { describe, expect, test, vi } from 'vitest';
import { shouldInstallMemoryLocalStorage } from './setupTests';

// A1: Node 26 会在 globalThis 上放一个实验性 localStorage getter；仅仅“读取”
// globalThis.localStorage（哪怕只是判断真假）就会触发它打印
// `ExperimentalWarning: localStorage is not available because --localstorage-file was not provided.`
// 修复方式是改用 Object.getOwnPropertyDescriptor 检查，绝不触发 getter。
// 这里直接测试被抽出的纯判断函数，不依赖真实 Node 版本即可验证契约。
describe('shouldInstallMemoryLocalStorage', () => {
  test('descriptor 缺失（属性不存在）→ 需要安装内存兜底', () => {
    expect(shouldInstallMemoryLocalStorage(undefined)).toBe(true);
  });

  test('只有 getter（模拟 Node 26 实验性 accessor）→ 需要兜底，且绝不触发 getter', () => {
    const getter = vi.fn(() => {
      throw new Error('getter 被调用：这正是 Node 26 打印 ExperimentalWarning 的路径');
    });
    const descriptor: PropertyDescriptor = {
      get: getter,
      configurable: true,
      enumerable: true,
    };
    expect(shouldInstallMemoryLocalStorage(descriptor)).toBe(true);
    expect(getter).not.toHaveBeenCalled();
  });

  test('value 是可用 Storage（如 jsdom 实现）→ 不需要兜底', () => {
    const usable: Storage = {
      length: 0,
      clear: () => {},
      getItem: () => null,
      key: () => null,
      removeItem: () => {},
      setItem: () => {},
    };
    const descriptor: PropertyDescriptor = { value: usable, writable: true, configurable: true };
    expect(shouldInstallMemoryLocalStorage(descriptor)).toBe(false);
  });

  test('value 存在但不是可用 Storage（缺 getItem 方法）→ 需要兜底', () => {
    const descriptor: PropertyDescriptor = { value: {}, writable: true, configurable: true };
    expect(shouldInstallMemoryLocalStorage(descriptor)).toBe(true);
  });

  test('访问 value 会抛异常的 descriptor 形态 → 需要兜底而不是向上抛异常', () => {
    const descriptor: PropertyDescriptor = {
      get value() {
        throw new Error('访问被拒绝');
      },
      configurable: true,
    } as unknown as PropertyDescriptor;
    expect(() => shouldInstallMemoryLocalStorage(descriptor)).not.toThrow();
    expect(shouldInstallMemoryLocalStorage(descriptor)).toBe(true);
  });
});

describe('setupTests 副作用', () => {
  test('setup 执行后全局 localStorage 可用（内存实现或 jsdom 实现）', () => {
    expect(typeof globalThis.localStorage.getItem).toBe('function');
    globalThis.localStorage.setItem('setup-test-key', 'v');
    expect(globalThis.localStorage.getItem('setup-test-key')).toBe('v');
    globalThis.localStorage.removeItem('setup-test-key');
  });
});
