import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// 非安全上下文（如 http://100.x.x.x）下 navigator.clipboard 不可用，需 execCommand 回退
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // 权限拒绝等场景继续走回退
    }
  }
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const input = focused instanceof HTMLInputElement || focused instanceof HTMLTextAreaElement ? focused : null;
  const inputRange = input && input.selectionStart !== null
    ? { start: input.selectionStart, end: input.selectionEnd, direction: input.selectionDirection }
    : null;
  const selection = document.getSelection();
  const ranges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange())
    : [];
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  try {
    ta.focus({ preventScroll: true });
    ta.select();
    const ok = document.execCommand('copy');
    if (!ok) {
      throw new Error('复制失败');
    }
  } finally {
    document.body.removeChild(ta);
    if (focused?.isConnected) {
      focused.focus({ preventScroll: true });
      if (input && inputRange) input.setSelectionRange(inputRange.start, inputRange.end, inputRange.direction ?? undefined);
    }
    if (selection && ranges.length) {
      selection.removeAllRanges();
      for (const range of ranges) {
        if (range.commonAncestorContainer.isConnected) selection.addRange(range);
      }
    }
  }
}
