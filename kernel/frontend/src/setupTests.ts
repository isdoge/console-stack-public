// vitest 环境兜底：Node 22+ 的实验性 localStorage 全局会遮蔽 jsdom 实现且值为 undefined，
// 此处统一替换为内存实现，保证测试在本地（新版 Node）与 CI（Node 20）行为一致。
//
// Node 26 起该全局是一个真正的 getter（backed by `--localstorage-file`）：只要「读取」
// globalThis.localStorage 的值（哪怕只是判断真假），未提供该 flag 时就会打印
// `ExperimentalWarning: localStorage is not available because --localstorage-file was not provided.`
// 因此判断必须只看 Object.getOwnPropertyDescriptor 的结果——它只反映属性的定义形态
// （data 还是 accessor），不会触发 accessor 的 get，从而绝不点燃这条警告。
export function shouldInstallMemoryLocalStorage(descriptor: PropertyDescriptor | undefined): boolean {
  try {
    const candidate = descriptor?.value as Storage | undefined;
    return !candidate || typeof candidate.getItem !== 'function';
  } catch {
    // descriptor 形态异常（理论上不会发生在真实 Object.getOwnPropertyDescriptor 返回值上）
    // 时保守选择安装内存兜底，不让 setup 本身炸掉整个测试文件。
    return true;
  }
}

const localStorageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
if (shouldInstallMemoryLocalStorage(localStorageDescriptor)) {
  const store = new Map<string, string>();
  const memoryStorage: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key) => (store.has(key) ? store.get(key)! : null),
    key: (index) => Array.from(store.keys())[index] ?? null,
    removeItem: (key) => {
      store.delete(key);
    },
    setItem: (key, value) => {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, 'localStorage', {
    value: memoryStorage,
    writable: true,
    configurable: true,
  });
}

// jsdom 未实现 ResizeObserver，而 C8-1 起总控台只剩 GridStack 一条渲染路径
// （`WidgetHost` 用它盯内容盒跑 resizeToContent），少了它网格一 init 就 ReferenceError。
// 这是测试环境缺口不是产品缺口（浏览器侧全线支持）——D2 时只有一个 describe 用得上，
// 现在渲染总控台的每个用例都要，按当时注释里的计划提到公共 setup。
if (typeof globalThis.ResizeObserver !== 'function') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// jsdom 未实现 matchMedia，侧栏主题初始化依赖它
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

export {};
