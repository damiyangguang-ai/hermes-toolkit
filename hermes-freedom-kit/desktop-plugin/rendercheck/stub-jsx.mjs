/** jsx-runtime stub：返回纯对象元素描述，供断言与结构检查。 */

export function jsx(type, props, key) {
  return { type, props: props || {}, key: key === undefined ? null : key }
}

export function jsxs(type, props, key) {
  return { type, props: props || {}, key: key === undefined ? null : key }
}

export const Fragment = Symbol.for('stub.fragment')

export default { jsx, jsxs, Fragment }
