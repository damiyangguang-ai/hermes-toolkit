/**
 * 用**真实** /api/presets 与 /api/config 数据，验证「是否需要风险确认」的判定。
 *
 * 目的：排除"我的假数据刚好能过、真实数据过不了"这种可能。
 *
 * 用法：node preset.detect.mjs <plugin.js> <baseUrl> <token> [preset]
 */

import { readFile, writeFile, mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const START = '// #region hfk-pure-core'
const END = '// #endregion hfk-pure-core'

const EXPORTS = [
  'createClient',
  'configFieldMap',
  'specsByPath',
  'presetMeta',
  'declaredPresetChanges',
  'buildSelection',
  'selectionMatchesDeclared',
  'detectActivePreset',
  'PRESET_KEYS'
]

async function loadPureCore(pluginPath) {
  const source = await readFile(pluginPath, 'utf8')
  const body = source.slice(source.indexOf(START), source.indexOf(END))
  const dir = await mkdtemp(join(tmpdir(), 'hfk-detect-'))
  const target = join(dir, 'pure.mjs')
  await writeFile(target, `${body}\n\nexport { ${EXPORTS.join(', ')} }\n`, 'utf8')
  return import(`file://${target.replace(/\\/g, '/')}`)
}

const [pluginPath, baseUrl, token, wanted = 'freedom'] = process.argv.slice(2)

const core = await loadPureCore(pluginPath)
const client = core.createClient({ baseUrl, token })

const presets = await client.presets()
const catalogue = await client.catalogue()
const config = await client.config()

const currentByPath = core.configFieldMap(config)
const detected = core.detectActivePreset(presets, catalogue, currentByPath)
const declared = core.declaredPresetChanges(presets, catalogue, wanted)
const selection = core.buildSelection(wanted, declared, catalogue, currentByPath)
const matches = core.selectionMatchesDeclared(selection, declared)
const meta = core.presetMeta(presets, wanted)

console.log('预设:', wanted)
console.log('requires_acknowledgement:', meta.requiresAcknowledgement)
console.log('detectActivePreset 结果 :', detected)
console.log('declared 条数           :', declared.length)
console.log('selectionMatchesDeclared:', matches)
console.log('=> requiresAck 应为      :', meta.requiresAcknowledgement === true && !(detected && detected === wanted && matches))

console.log('\n逐项比对（declared vs 当前 config）：')
let mismatch = 0
for (const change of declared) {
  const path = String(change.path)
  const row = currentByPath[path]
  const same =
    row && row.exists && String(row.value) === String(change.value) ? 'OK' : 'DIFF'
  if (row && row.exists && String(row.value) === String(change.value)) {
    // 数值/布尔也用严格比较复核
  } else {
    mismatch += 1
  }
  console.log(
    `  ${same.padEnd(4)} ${path}\n         当前=${JSON.stringify(row ? row.value : null)} (exists=${row ? row.exists : 'n/a'})  预设=${JSON.stringify(change.value)}`
  )
}
console.log(`\n不一致项: ${mismatch}`)
