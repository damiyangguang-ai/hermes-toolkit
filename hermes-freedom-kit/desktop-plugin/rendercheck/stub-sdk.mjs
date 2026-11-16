/** @hermes/plugin-sdk stub：只提供插件用到的那 22 个符号。 */

const component = name => {
  const Component = () => null
  Component.displayName = `Stub(${name})`
  return Component
}

export const Badge = component('Badge')
export const Button = component('Button')
export const Checkbox = component('Checkbox')
export const Codicon = component('Codicon')
export const EmptyState = component('EmptyState')
export const Input = component('Input')
export const Select = component('Select')
export const SelectContent = component('SelectContent')
export const SelectItem = component('SelectItem')
export const SelectTrigger = component('SelectTrigger')
export const SelectValue = component('SelectValue')
export const Separator = component('Separator')
export const Switch = component('Switch')
export const Tabs = component('Tabs')
export const TabsList = component('TabsList')
export const TabsTrigger = component('TabsTrigger')
export const Textarea = component('Textarea')

export const ROUTES_AREA = 'stub.routes'
export const SIDEBAR_NAV_AREA = 'stub.sidebar'

export function atom(initial) {
  let value = initial
  return {
    get: () => value,
    set: next => {
      value = next
    },
    subscribe: () => () => {}
  }
}

export function useValue(handle) {
  return handle && typeof handle.get === 'function' ? handle.get() : undefined
}

export const host = {
  status: async () => ({ hermes_home: '' }),
  request: async () => null,
  logs: async () => []
}
