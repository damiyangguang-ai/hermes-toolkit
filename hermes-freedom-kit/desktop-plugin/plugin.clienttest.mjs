/**
 * 插件数据层集成测试（Node 侧）。
 *
 * 目的：验证 plugin.js 里 `#region hfk-pure-core` 这一段——尤其是 HTTP 客户端——
 * 能真正打通 HFK Core 服务，而不是只在 Electron 里"看起来能跑"。
 *
 * 做法：
 *   1. 从 plugin.js 里**原样抽取** hfk-pure-core 区段（这段按设计不依赖 React/SDK）；
 *   2. 补上 export，写成临时 .mjs 并 import；
 *   3. 对着一个**真实运行的** HFK Core 服务（由 scripts/plugin_e2e.py 启动）跑断言。
 *
 * 用法：
 *   node plugin.clienttest.mjs <plugin.js 路径> <baseUrl> <token>
 */

import { readFile, writeFile, mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const START = '// #region hfk-pure-core'
const END = '// #endregion hfk-pure-core'

/** 需要从区段里导出的符号。 */
const EXPORTS = [
  'createClient',
  'parseServiceDescriptor',
  'normalizeBaseUrl',
  'baseUrlFromPort',
  'configFieldMap',
  'specsByPath',
  'presetMeta',
  'declaredPresetChanges',
  'buildSelection',
  'buildOverrides',
  'coerceValue',
  'validateFieldValue',
  'collectSelectionErrors',
  'previewSummary',
  'transactionMeta',
  'fmtValue',
  'fmtBytes',
  'fmtTimestamp',
  'PRESET_KEYS',
  'API_PATHS',
  'detectActivePreset',
  'selectionMatchesDeclared'
]

let passes = 0
const failures = []

function check(name, ok, detail) {
  if (ok) {
    passes += 1
    console.log(`  ✔ ${name}`)
  } else {
    failures.push(`${name}${detail ? `  ${detail}` : ''}`)
    console.log(`  ✘ ${name}${detail ? `  ${detail}` : ''}`)
  }
}

/** 从 plugin.js 抽出纯逻辑区段，做成可 import 的模块。 */
async function loadPureCore(pluginPath) {
  const source = await readFile(pluginPath, 'utf8')
  const startIndex = source.indexOf(START)
  const endIndex = source.indexOf(END)
  if (startIndex < 0 || endIndex < 0 || endIndex <= startIndex) {
    throw new Error('在 plugin.js 里找不到 hfk-pure-core 区段标记')
  }
  const body = source.slice(startIndex, endIndex)
  const moduleSource = `${body}\n\nexport { ${EXPORTS.join(', ')} }\n`
  const dir = await mkdtemp(join(tmpdir(), 'hfk-plugin-core-'))
  const target = join(dir, 'pure-core.mjs')
  await writeFile(target, moduleSource, 'utf8')
  return import(`file://${target.replace(/\\/g, '/')}`)
}

async function main() {
  const [pluginPath, baseUrl, token] = process.argv.slice(2)
  if (!pluginPath || !baseUrl || !token) {
    console.error('用法: node plugin.clienttest.mjs <plugin.js> <baseUrl> <token>')
    return 2
  }

  console.log('阶段 A｜抽取纯逻辑区段')
  const core = await loadPureCore(pluginPath)
  check('区段可独立 import（不依赖 React/SDK）', true)
  for (const name of ['createClient', 'buildSelection', 'previewSummary']) {
    check(`导出 ${name}`, typeof core[name] === 'function')
  }

  console.log('阶段 B｜纯函数语义')
  const selection = core.buildSelection(
    'balanced',
    [{ path: 'approvals.timeout', value: 120 }],
    {
      fields: [
        { path: 'approvals.timeout', kind: 'int', default: 300 },
        { path: 'approvals.mode', kind: 'enum', default: 'smart' }
      ]
    },
    { 'approvals.timeout': { exists: true, value: 60 } }
  )
  check(
    'buildSelection 只为预设声明的字段置 enabled',
    selection['approvals.timeout'].enabled === true && selection['approvals.mode'].enabled === false
  )
  check('buildSelection 记录当前值', selection['approvals.timeout'].value === 120)
  const overrides = core.buildOverrides(
    { fields: [{ path: 'approvals.timeout', kind: 'int' }, { path: 'approvals.mode', kind: 'enum' }] },
    selection
  )
  check('buildOverrides 只输出已启用字段', Object.keys(overrides).length === 1 && overrides['approvals.timeout'] === 120)
  check(
    'validateFieldValue 拒绝非法枚举',
    core.validateFieldValue({ path: 'approvals.mode', kind: 'enum', values: ['manual', 'smart', 'off'] }, 'deny').length > 0
  )
  check(
    'validateFieldValue 接受合法枚举',
    core.validateFieldValue({ path: 'approvals.mode', kind: 'enum', values: ['manual', 'smart', 'off'] }, 'off').length === 0
  )
  check('normalizeBaseUrl 拒绝非回环', core.normalizeBaseUrl('http://198.51.100.7:9000').ok === false)

  // ---- 预设「当前生效」推断：修的是「切走再回来跳回默认档」这个真问题 ----
  const fakeCatalogue = {
    fields: [
      { path: 'approvals.mode', kind: 'enum', values: ['manual', 'smart', 'off'], default: 'smart' },
      { path: 'approvals.cron_mode', kind: 'enum', values: ['deny', 'approve'], default: 'deny' }
    ]
  }
  const fakePresets = {
    default: 'balanced',
    items: {
      conservative: { label: '保守', changes: [{ path: 'approvals.mode', value: 'manual' }] },
      balanced: { label: '平衡', changes: [{ path: 'approvals.mode', value: 'smart' }] },
      freedom: {
        label: '自由',
        changes: [
          { path: 'approvals.mode', value: 'off' },
          { path: 'approvals.cron_mode', value: 'approve' }
        ]
      },
      custom: { label: '自定义', changes: [] }
    }
  }
  const asMap = (mode, cron) => ({
    'approvals.mode': { exists: true, value: mode },
    'approvals.cron_mode': { exists: true, value: cron }
  })
  check(
    'detectActivePreset: 全项匹配 → freedom',
    core.detectActivePreset(fakePresets, fakeCatalogue, asMap('off', 'approve')) === 'freedom'
  )
  check(
    'detectActivePreset: 只匹配平衡 → balanced',
    core.detectActivePreset(fakePresets, fakeCatalogue, asMap('smart', 'deny')) === 'balanced'
  )
  check(
    'detectActivePreset: 混合状态 → null（自定义组合）',
    core.detectActivePreset(fakePresets, fakeCatalogue, asMap('off', 'deny')) === null
  )

  // ---- 风险确认是否需要：预设已生效且没改写时不该再要求确认 ----
  const declaredFreedom = [{ path: 'approvals.mode', value: 'off' }, { path: 'approvals.cron_mode', value: 'approve' }]
  const untouched = {
    'approvals.mode': { enabled: true, value: 'off' },
    'approvals.cron_mode': { enabled: true, value: 'approve' },
    'approvals.timeout': { enabled: false, value: 60 }
  }
  check('selectionMatchesDeclared: 原样 → true', core.selectionMatchesDeclared(untouched, declaredFreedom) === true)

  const tweaked = { ...untouched, 'approvals.mode': { enabled: true, value: 'smart' } }
  check('selectionMatchesDeclared: 改过值 → false', core.selectionMatchesDeclared(tweaked, declaredFreedom) === false)

  const extra = { ...untouched, 'approvals.timeout': { enabled: true, value: 90 } }
  check('selectionMatchesDeclared: 多开了字段 → false', core.selectionMatchesDeclared(extra, declaredFreedom) === false)

  console.log('阶段 C｜真实 HTTP（无令牌必须 401）')
  const noToken = core.createClient({ baseUrl })
  let unauthorized = false
  try {
    await noToken.status()
  } catch (error) {
    unauthorized = String(error.code) === 'unauthorized'
  }
  check('不带令牌访问 /api/status 被拒', unauthorized)

  console.log('阶段 D｜真实 HTTP（带令牌走完整链路）')
  const client = core.createClient({ baseUrl, token })
  const health = await client.health()
  check('/api/health 免令牌可用', health && health.ok === true)
  const env = await client.env()
  check('/api/env 返回 0.23.1', env && env.version === '0.23.1', `实际 ${env && env.version}`)
  check('环境含兼容性结论', Boolean(env.compat && env.compat.supported === true))
  const status = await client.status()
  check('/api/status 有 hfk_version', Boolean(status && status.hfk_version))
  const catalogue = await client.catalogue()
  check('字段清单非空', Array.isArray(catalogue.fields) && catalogue.fields.length > 0)
  check('清单含 C 类阻断项说明', Array.isArray(catalogue.blocked))
  const presets = await client.presets()
  check('四个预设齐备', core.PRESET_KEYS.every(key => presets.items[key]))
  const config = await client.config()
  const fieldMap = core.configFieldMap(config)
  check('受管投影不含 document（无全量配置泄露）', config.document === undefined)
  check('受管投影含 approvals.mode', Boolean(fieldMap['approvals.mode']))

  const preview = await client.preview({ preset: 'balanced' })
  const summary = core.previewSummary(preview)
  check('预览返回 changes 数组', Array.isArray(preview.changes))
  check('预览标记 has_effect', typeof preview.has_effect === 'boolean')
  check('预览每项都有 before/after', preview.changes.every(item => 'before' in item && 'after' in item))

  const applied = await client.apply({ items: { 'approvals.timeout': 120 } })
  check('apply 返回 transaction_id', Boolean(applied.transaction_id))
  check('apply 记录了字段级变更', applied.changes[0].before === 60 && applied.changes[0].after === 120)
  const afterApply = core.configFieldMap(await client.config())
  check('apply 后服务端读到 timeout=120', afterApply['approvals.timeout'].value === 120)

  const restored = await client.restore({})
  check('restore 状态 restored', restored.status === 'restored', `实际 ${restored.status}`)
  const afterRestore = core.configFieldMap(await client.config())
  check('restore 后回到 60', afterRestore['approvals.timeout'].value === 60)

  console.log('')
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：`)
    for (const item of failures) console.log(`  · ${item}`)
    return 1
  }
  console.log(`全部通过：${passes} 项检查`)
  return 0
}

main()
  .then(code => process.exit(code))
  .catch(error => {
    console.error('测试脚本异常：', error)
    process.exit(3)
  })
