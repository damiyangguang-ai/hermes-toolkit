/** React stub：只为了让组件函数体能被执行一次，不模拟真实渲染。 */

export function useState(initial) {
  const value = typeof initial === 'function' ? initial() : initial
  return [value, () => {}]
}

export function useEffect() {
  /* 不执行副作用：静态冒烟只需要组件首帧 */
}

export function useLayoutEffect() {}

export function useRef(initial) {
  return { current: initial === undefined ? null : initial }
}

export function useCallback(fn) {
  return fn
}

export function useMemo(fn) {
  return fn()
}

export function useReducer(reducer, initial) {
  return [initial, () => {}]
}

export function useContext() {
  return null
}

export function useId() {
  return 'stub-id'
}

export default {
  useState,
  useEffect,
  useLayoutEffect,
  useRef,
  useCallback,
  useMemo,
  useReducer,
  useContext,
  useId
}
