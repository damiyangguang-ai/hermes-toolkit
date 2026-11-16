/**
 * 运行期冒烟测试：不装 React、不开 Electron，直接执行插件里的渲染函数。
 *
 * 为什么需要它：`node --check` 只验语法，查不出「把变量用在了没传入的作用域里」
 * 这类错误（真实事故：makeView 里用了 activePreset，但没从 state 解构出来，
 * 结果页面报 `activePreset is not defined`）。
 *
 * 做法：
 *   1. 读 plugin.js，把三个 import 改写到本地 stub；
 *   2. 追加导出，把内部函数暴露出来；
 *   3. 用一份贴近真实 API 的假数据调用 makeView + 各 render* 函数 + 组件函数体。
 *
 * 任何未定义标识符都会在 import/执行时抛出 ReferenceError → 测试失败。
 *
 * 用法：node rendercheck/render.test.mjs ../plugin.js
 */

import { readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as sdk from './stub-sdk.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))

/** 深度遍历 jsx 元素树。 */
function walk(node, visit) {
  if (!node || typeof node !== 'object') {
    return
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      walk(child, visit)
    }
    return
  }
  visit(node)
  const props = node.props || {}
  if ('children' in props) {
    walk(props.children, visit)
  }
}

/** 树里是否存在「自由预设风险确认」勾选框。 */
function hasAckCheckbox(tree) {
  let found = false
  walk(tree, node => {
    if (node.type === sdk.Checkbox && node.props && node.props['aria-label'] === '确认自由预设的风险') {
      found = true
    }
  })
  return found
}

const EXPORTS = [
  'makeView',
  'renderStatusHeader',
  'renderPresetSection',
  'renderFieldSection',
  'renderPatchSection',
  'renderHistorySection',
  'HfkPage',
  'HfkSettingsPage',
  'detectActivePreset',
  'selectionMatchesDeclared',
  'previewSummary'
]

let pass = 0
const fail = []

function ok(name, condition, detail) {
  if (condition) {
    pass += 1
    console.log(`  ✔ ${name}`)
  } else {
    fail.push(name)
    console.log(`  ✘ ${name}${detail ? `  ${detail}` : ''}`)
  }
}

async function buildModule(pluginPath) {
  const source = await readFile(pluginPath, 'utf8')
  const rewritten = source
    .replace(/from '@hermes\/plugin-sdk'/g, "from './stub-sdk.mjs'")
    .replace(/from 'react\/jsx-runtime'/g, "from './stub-jsx.mjs'")
    .replace(/from 'react'/g, "from './stub-react.mjs'")
  const target = join(HERE, 'plugin.under-test.mjs')
  await writeFile(target, `${rewritten}\n\nexport { ${EXPORTS.join(', ')} }\n`, 'utf8')
  return import(`file://${target.replace(/\\/g, '/')}`)
}

/** 贴近真实 /api/* 响应的假 store。 */
function fakeStore() {
  return {
    env: {
      version: '0.23.1',
      version_source: 'install-stamp.json',
      profile: 'default',
      home: 'C:\\Users\\x\\AppData\\Local\\hermes',
      hfk_dir: 'C:\\Users\\x\\AppData\\Local\\hermes\\hfk',
      config_readable: true,
      git_dirty: true,
      config_error: null,
      warnings: ['安装目录存在未提交的本地修改（git dirty）'],
      gateway: { running: true, pid: 1234, state: 'running', active_agents: 0 },
      compat: { supported: true, exact: true, reason: '精确匹配已验证版本 0.23.1' }
    },
    status: {
      hfk_version: '0.1.0',
      hermes_version: '0.23.1',
      supported: true,
      compat_reason: 'ok',
      profile: 'default',
      pending_transactions: 1,
      total_transactions: 2,
      backups: 2,
      last_apply: '2026-11-16T08:16:34+00:00',
      last_restore: null,
      active_fields: { 'approvals.mode': 'off', 'approvals.timeout': 60 }
    },
    catalogue: {
      fields: [
        { path: 'approvals.mode', title: '审批主模式', kind: 'enum', values: ['manual', 'smart', 'off'], default: 'smart', risk: 'A', effect: '…', surfaces: ['CLI'], evidence: 'e1' },
        { path: 'approvals.timeout', title: '超时', kind: 'int', default: 300, risk: 'A', effect: '…', surfaces: [], evidence: 'e2' },
        { path: 'approvals.mcp_reload_confirm', title: '重载确认', kind: 'bool', default: true, risk: 'A', effect: '…', surfaces: [], evidence: 'e3' },
        { path: 'approvals.denial_breaker_threshold', title: '熔断', kind: 'int', default: 3, risk: 'A', effect: '…', surfaces: [], evidence: 'e4' },
        { path: 'command_allowlist', title: '永久放行', kind: 'list', default: [], risk: 'B', effect: '…', surfaces: [], evidence: 'e5' },
        { path: 'approvals.deny', title: '黑名单', kind: 'list', default: [], risk: 'B', effect: '…', surfaces: [], evidence: 'e6' }
      ],
      blocked: [{ path: 'security.approval.transport', reason: '安全边界' }],
      code_level_floors: [{ name: 'HARDLINE', detail: '硬阻断' }]
    },
    presets: {
      default: 'balanced',
      items: {
        conservative: { label: '保守', description: 'd', risk_note: '', requires_acknowledgement: false, changes: [{ path: 'approvals.mode', value: 'manual' }] },
        balanced: { label: '平衡', description: 'd', risk_note: '', requires_acknowledgement: false, changes: [{ path: 'approvals.timeout', value: 120 }] },
        freedom: {
          label: '自由',
          description: 'd',
          risk_note: 'r',
          requires_acknowledgement: true,
          changes: [
            { path: 'approvals.mode', value: 'off' },
            { path: 'approvals.timeout', value: 60 },
            { path: 'approvals.mcp_reload_confirm', value: false },
            { path: 'approvals.denial_breaker_threshold', value: 0 }
          ]
        },
        custom: { label: '自定义', description: 'd', risk_note: '', requires_acknowledgement: false, changes: [] }
      }
    },
    config: {
      readable: true,
      fields: {
        'approvals.mode': { exists: true, value: 'off' },
        'approvals.timeout': { exists: true, value: 60 },
        'approvals.mcp_reload_confirm': { exists: true, value: false },
        'approvals.denial_breaker_threshold': { exists: true, value: 0 },
        'command_allowlist': { exists: true, value: ['recursive delete'] },
        'approvals.deny': { exists: false, value: null }
      }
    },
    transactions: {
      count: 2,
      transactions: [
        {
          transaction_id: 'tx-1',
          status: 'applied',
          preset: 'freedom',
          timestamp: '2026-11-16T08:16:34+00:00',
          hermes_version: '0.23.1',
          profile: 'default',
          backup_path: 'C:\\b\\1.yaml',
          changes: [{ path: 'approvals.mode', before: 'smart', before_exists: true, after: 'off', status: 'applied', note: '' }]
        }
      ]
    },
    backups: { directory: 'C:\\b', count: 1, files: [{ name: 'config-1.yaml', size: 18800 }] }
  }
}

function fakeHandlers() {
  return {
    onRefresh: () => {},
    onSelectPreset: () => {},
    onToggleField: () => {},
    onChangeField: () => {},
    onFieldError: () => {},
    onPreview: () => {},
    onApply: () => {},
    onRestore: () => {},
    onUninstall: () => {},
    onAck: () => {},
    onDismissNotice: () => {},
    onSelectHistoryTab: () => {}
  }
}

function selectionFor(store, declared) {
  const map = {}
  for (const change of declared) {
    map[change.path] = { enabled: true, value: change.value, fromPreset: true }
  }
  for (const spec of store.catalogue.fields) {
    if (!map[spec.path]) {
      map[spec.path] = { enabled: false, value: spec.default, fromPreset: false }
    }
  }
  return map
}

async function main() {
  const pluginPath = process.argv[2] || join(HERE, '..', 'plugin.js')
  console.log('阶段 A｜模块可被真实执行（import 时就会暴露未定义标识符）')
  const mod = await buildModule(pluginPath)
  ok('plugin.js 可加载', true)
  for (const name of EXPORTS) {
    ok(`导出 ${name}`, typeof mod[name] === 'function')
  }

  const store = fakeStore()
  const declared = mod === null ? [] : store.presets.items.freedom.changes
  const handlers = fakeHandlers()

  console.log('阶段 B｜makeView：这就是 `activePreset is not defined` 的发生地')
  let view = null
  try {
    view = mod.makeView(
      {
        store,
        presetPreviews: { freedom: { changes: [], has_effect: false, warnings: [] } },
        preset: 'freedom',
        selection: selectionFor(store, declared),
        ack: false,
        busy: null,
        error: null,
        notice: null,
        preview: null,
        restoreReport: null,
        historyTab: 'backups',
        conn: { baseUrl: 'http://127.0.0.1:1', source: 'x', hfkVersion: '0.1.0' },
        loadErrors: [],
        activePreset: 'freedom'
      },
      handlers
    )
    ok('makeView 不抛异常', true)
  } catch (error) {
    ok('makeView 不抛异常', false, String(error && error.message))
    throw error
  }
  ok('view 携带 activePreset', view.activePreset === 'freedom')
  ok('已生效且未改写 → 不要求风险确认', view.requiresAck === false, `实际 ${view.requiresAck}`)

  const tweaked = selectionFor(store, declared)
  tweaked['approvals.mode'] = { enabled: true, value: 'smart' }
  const view2 = mod.makeView(
    {
      store,
      presetPreviews: {},
      preset: 'freedom',
      selection: tweaked,
      ack: false,
      busy: null,
      error: null,
      notice: null,
      preview: null,
      restoreReport: null,
      historyTab: 'backups',
      conn: null,
      loadErrors: [],
      activePreset: 'freedom'
    },
    handlers
  )
  ok('改过值 → 重新要求风险确认', view2.requiresAck === true, `实际 ${view2.requiresAck}`)

  console.log('阶段 C｜各区块渲染函数（含空数据与有数据两种）')
  for (const [name, fn] of [
    ['renderStatusHeader', mod.renderStatusHeader],
    ['renderPresetSection', mod.renderPresetSection],
    ['renderFieldSection', mod.renderFieldSection],
    ['renderPatchSection', mod.renderPatchSection],
    ['renderHistorySection', mod.renderHistorySection]
  ]) {
    try {
      const out = fn(view)
      ok(`${name} 返回元素`, out !== undefined && out !== null)
    } catch (error) {
      ok(`${name} 返回元素`, false, String(error && error.message))
    }
  }

  // 关键回归：已生效且无写入时，卡片里**不能**出现风险确认勾选框。
  // （真实事故：勾选框只判断了「选中」没判断「是否需要确认」，导致
  //  改动 0 项时仍要求勾选，且勾了又像是"自己没了"。）
  try {
    const presetTree = mod.renderPresetSection(view)
    ok('已生效时卡片不含风险确认勾选框', hasAckCheckbox(presetTree) === false)
  } catch (error) {
    ok('已生效时卡片不含风险确认勾选框', false, String(error && error.message))
  }
  try {
    const presetTree2 = mod.renderPresetSection(view2)
    ok('需要写入时卡片必须出现风险确认勾选框', hasAckCheckbox(presetTree2) === true)
  } catch (error) {
    ok('需要写入时卡片必须出现风险确认勾选框', false, String(error && error.message))
  }

  // 空 store（首帧/部分接口失败）不应崩
  const emptyStore = { env: null, status: null, catalogue: null, presets: null, config: null, transactions: null, backups: null }
  try {
    const emptyView = mod.makeView(
      { store: emptyStore, presetPreviews: {}, preset: 'balanced', selection: {}, ack: false, busy: 'connect', error: null, notice: null, preview: null, restoreReport: null, historyTab: 'backups', conn: null, loadErrors: [], activePreset: '' },
      handlers
    )
    mod.renderStatusHeader(emptyView)
    mod.renderPresetSection(emptyView)
    mod.renderFieldSection(emptyView)
    mod.renderPatchSection(emptyView)
    mod.renderHistorySection(emptyView)
    ok('空数据下所有区块也不崩', true)
  } catch (error) {
    ok('空数据下所有区块也不崩', false, String(error && error.message))
  }

  console.log('阶段 D｜组件函数体能执行（hooks 用 stub）')
  for (const [name, component] of [
    ['HfkPage', mod.HfkPage],
    ['HfkSettingsPage', mod.HfkSettingsPage]
  ]) {
    try {
      component({ storage: { get: () => null, set: () => {} } })
      ok(`${name} 首帧不抛异常`, true)
    } catch (error) {
      ok(`${name} 首帧不抛异常`, false, String(error && error.message))
    }
  }

  console.log('')
  if (fail.length) {
    console.log(`失败 ${fail.length} 项：`)
    for (const item of fail) console.log(`  · ${item}`)
    return 1
  }
  console.log(`全部通过：${pass} 项`)
  return 0
}

main()
  .then(code => process.exit(code))
  .catch(error => {
    console.error('冒烟测试异常：', error)
    process.exit(3)
  })
