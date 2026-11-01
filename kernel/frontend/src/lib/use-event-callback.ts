import { useCallback, useRef } from 'react';

/**
 * 稳定引用的事件回调：每次渲染把最新实现存进 ref，返回的引用恒定。
 * 用途：effect 依赖数组需要「最新的函数」但不希望函数引用变化触发重跑，
 * 替代在依赖数组里直接写每次渲染都重建的函数（react-hooks/exhaustive-deps）。
 */
export function useEventCallback<Args extends unknown[], R>(
  fn: (...args: Args) => R,
): (...args: Args) => R {
  const ref = useRef(fn);
  ref.current = fn;
  return useCallback((...args: Args) => ref.current(...args), []);
}
