import { afterEach, describe, expect, test, vi } from 'vitest';
import { copyText } from './utils';

// A2：copyText 目前忽略 document.execCommand('copy') 的布尔返回值——浏览器拒绝复制
// （返回 false，不抛异常）时，调用方的 `.then(() => toast.success(...))` 仍会触发，
// 用户看到「复制成功」但剪贴板其实是空的，且不可诊断。
//
// jsdom 既没有真实 navigator.clipboard 也没有真实 document.execCommand（均为
// undefined），所以这里显式桩替两者，覆盖 true / false / throw 三种回退路径的
// 契约，以及 textarea 清理是否发生。

function stubExecCommand(impl: () => boolean) {
  const fn = vi.fn(impl);
  Object.defineProperty(document, 'execCommand', {
    value: fn,
    writable: true,
    configurable: true,
  });
  return fn;
}

function stubClipboardWriteText(writeText?: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, 'clipboard', {
    value: writeText ? { writeText } : undefined,
    writable: true,
    configurable: true,
  });
}

function textareaCount(): number {
  return document.querySelectorAll('textarea').length;
}

describe('copyText', () => {
  afterEach(() => {
    stubClipboardWriteText(undefined);
    Object.defineProperty(document, 'execCommand', {
      value: undefined,
      writable: true,
      configurable: true,
    });
    document.body.innerHTML = '';
  });

  test('navigator.clipboard.writeText 成功 → resolve，不触碰 execCommand 回退', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboardWriteText(writeText);
    const execCommand = stubExecCommand(() => true);

    await expect(copyText('hello')).resolves.toBeUndefined();

    expect(writeText).toHaveBeenCalledWith('hello');
    expect(execCommand).not.toHaveBeenCalled();
  });

  test('clipboard 不可用、execCommand 返回 true → resolve 且清理 textarea', async () => {
    stubClipboardWriteText(undefined);
    const execCommand = stubExecCommand(() => true);

    await expect(copyText('hi')).resolves.toBeUndefined();

    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(textareaCount()).toBe(0);
  });

  test('execCommand 返回 false → 必须 reject，不能被调用方当作成功；仍清理 textarea', async () => {
    stubClipboardWriteText(undefined);
    stubExecCommand(() => false);

    await expect(copyText('bye')).rejects.toThrow('复制失败');

    expect(textareaCount()).toBe(0);
  });

  test('execCommand 抛异常 → reject 原始异常；仍清理 textarea', async () => {
    stubClipboardWriteText(undefined);
    const boom = new Error('boom');
    stubExecCommand(() => {
      throw boom;
    });

    await expect(copyText('x')).rejects.toBe(boom);

    expect(textareaCount()).toBe(0);
  });

  test('clipboard.writeText 抛异常 → 继续走 execCommand 回退且能成功', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    stubClipboardWriteText(writeText);
    const execCommand = stubExecCommand(() => true);

    await expect(copyText('fallback')).resolves.toBeUndefined();

    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(textareaCount()).toBe(0);
  });

  test.each(['success', 'false', 'throw'])('fallback %s 后恢复原输入焦点与选区', async (result) => {
    stubClipboardWriteText(undefined);
    const input = document.createElement('input');
    input.value = 'original draft';
    document.body.appendChild(input);
    input.focus();
    input.setSelectionRange(2, 7, 'backward');
    stubExecCommand(() => {
      // jsdom 的 select 不像真实浏览器那样转移焦点，在剪贴板桩模拟。
      document.querySelector('textarea')!.focus();
      if (result === 'throw') throw new Error('denied');
      return result === 'success';
    });
    if (result === 'success') await copyText('copied');
    else await expect(copyText('copied')).rejects.toThrow();
    expect(document.activeElement).toBe(input);
    expect([input.selectionStart, input.selectionEnd, input.selectionDirection]).toEqual([2, 7, 'backward']);
    expect(input.value).toBe('original draft');
    expect(textareaCount()).toBe(0);
  });
});
