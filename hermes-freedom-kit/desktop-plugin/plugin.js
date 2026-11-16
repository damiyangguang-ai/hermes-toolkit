/**
 * HFK · Hermes Freedom Kit —— Hermes Desktop 插件（单文件，无构建步骤）
 * =============================================================================
 *
 * 这个文件就是插件本体。安装位置（文件名必须叫 plugin.js）：
 *
 *   <HERMES_HOME>\desktop-plugins\hfk\plugin.js
 *
 * 本机 <HERMES_HOME> = <HERMES_HOME>
 *
 * 运行方式（Hermes Desktop 0.23.1 实测契约）：
 *   · 没有 manifest 文件，默认导出就是 manifest：
 *     { id, name, description, defaultEnabled, capabilities, register(ctx) }
 *   · 插件在 Electron renderer 里以 blob ESM 方式加载（sandbox + contextIsolation）；
 *     只能 import '@hermes/plugin-sdk'、'react'、'react/jsx-runtime'。
 *   · JSX 不会被编译，所有 UI 都用 jsx()/jsxs() 手写。
 *   · 贡献通过 ctx.register / ctx.registerMany / ctx.registerSettingsPage 注册，
 *     返回的 disposer 在插件卸载/热重载时清理。
 *
 * 设计边界（和 Python 核心的分工）：
 *   · 插件只做 UI 与 HTTP 调用，**绝不自己读写 config.yaml**（renderer 没有 fs）；
 *     真正的备份、字段级写入、事务、回滚都在 HFK Core 本地服务里完成。
 *   · 服务只监听 127.0.0.1，每个请求必须带 X-HFK-Token。令牌来自
 *     <HERMES_HOME>\hfk\service.json（权限 0600），插件只在内存里使用它：
 *     **不显示、不持久化、不写日志、不进错误信息**。
 *   · /api/config 只返回「HFK 受管字段投影」（fields: {path:{exists,value}}），
 *     不会返回整份 config.yaml —— 用户写在里面的 API Key 等秘密插件永远看不到。
 *   · 所有写入前必须先拿到当前 selection 的 /api/preview 结果，Apply 需要二次确认；
 *     绝不提前宣称补丁成功，只认 /api/apply 的返回。
 *
 * 本文件结构（从上到下）：
 *   1. imports
 *   2. #region hfk-pure-core —— 纯函数 + HTTP 客户端（不依赖 React/SDK，
 *      plugin.selftest.js / plugin.mocktest.js 会抽取这一段做独立测试）
 *   3. UI 设计令牌与小型展示组件
 *   4. 连接发现 / 数据加载辅助函数
 *   5. HfkPage —— 主页面（状态条 / 预设卡片 / 逐项配置 / 补丁管理 / 历史页签）
 *   6. HfkSettingsPage + 连接覆盖表单（Settings ▸ Plugins）
 *   7. 默认导出（manifest + register）
 */

import {
  Badge,
  Button,
  Checkbox,
  Codicon,
  EmptyState,
  Input,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Separator,
  Switch,
  Tabs,
  TabsList,
  TabsTrigger,
  Textarea,
  atom,
  host,
  useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// =============================================================================
// 1) #region hfk-pure-core —— 纯函数与 HTTP 客户端
// -----------------------------------------------------------------------------
// 约束：这一段里**不能**出现 React / SDK / DOM 引用，所有函数都是
// 输入 → 输出的纯逻辑（HTTP 客户端除外，它只依赖全局 fetch）。
// 这样 `node plugin.selftest.js` 可以把这段抽到临时 .mjs 里直接 import 测试。
//
// HTTP 契约（与 hfk/service/app.py 逐条对齐）：
//   GET  /api/health          { ok, hfk_version }                     无需令牌
//   GET  /api/env            环境报告 + compat
//   GET  /api/status         补丁状态视图
//   GET  /api/catalogue      字段清单 / 阻断字段 / 代码级底线
//   GET  /api/presets        四个预设及其逐项变更
//   GET  /api/transactions   事务历史
//   GET  /api/backups        备份文件列表
//   GET  /api/config         { readable, fields: { path: {exists, value} } }（受管投影）
//   POST /api/preview        { preset?, overrides?, items? }
//   POST /api/apply          { preset?, overrides?, items?, acknowledge_risk? }
//   POST /api/restore        { transaction_id? }
//   POST /api/uninstall      { remove_plugin? }
// =============================================================================
// #region hfk-pure-core

/** 本机 Hermes Home 的兜底默认值；优先用 host.status().hermes_home 覆盖。 */
const DEFAULT_HERMES_HOME = 'C:\\Users\\Administrator\\AppData\\Local\\hermes'

/** 服务发现文件相对 Hermes Home 的位置（与 Python 端 hfk/service/app.py 一致）。 */
const SERVICE_FILE_RELATIVE = 'hfk/service.json'

/** 「服务未启动」空状态里展示给用户的启动命令。 */
const SERVICE_COMMAND_PYTHON = 'python -m hfk.cli service'
const SERVICE_COMMAND_CLI = 'hfk service'

/**
 * UI 构建标记。
 *
 * 每次改动插件都必须同步改这里。用途：装完之后一眼就能确认 Hermes 渲染进程
 * 到底加载的是新版本还是旧版本——「文件已替换但页面还跑旧模块」是真实踩过的坑
 * （替换文件不一定触发热重载，需要禁用/启用插件或重启）。
 */
const UI_BUILD = '2026-11-16 17:20 · ack-gate v2（勾选框仅在真的需要写入时出现）'

/** 四个预设的固定顺序（与 hfk/profiles/presets.py 的 PRESET_ORDER 一致）。 */
const PRESET_KEYS = ['conservative', 'balanced', 'freedom', 'custom']

/** /api/presets 拿不到时的兜底文案（绝不冒充真实数据，只用于可读降级）。 */
const PRESET_META_FALLBACK = {
  conservative: {
    label: '保守',
    description: '几乎不改变现有行为：危险命令逐个询问，非交互场景一律拒绝。',
    risk_note: '',
    requires_acknowledgement: false
  },
  balanced: {
    label: '平衡（默认）',
    description: '保留智能审批与非交互拒绝策略，只去掉明显冗余的二次确认并收紧超时。',
    risk_note: '不改变任何「拦不拦」的判断，只改变「问几次、等多久」。',
    requires_acknowledgement: false
  },
  freedom: {
    label: '自由',
    description: '审批主模式切到 off（等同 --yolo），并放开 cron / 单次查询 / 无人值守。',
    risk_note: '危险命令不再逐条询问；无人值守平台将完全无人把关。仅在你能接受 Hermes 自主执行时使用。',
    requires_acknowledgement: true
  },
  custom: {
    label: '自定义',
    description: '不使用任何预设，只应用你在下面逐项勾选的字段。',
    risk_note: '',
    requires_acknowledgement: false
  }
}

/** 风险分级 A/B/C 的展示信息（A 绿、B 橙、C 灰且不可交互）。 */
const RISK_META = {
  A: { label: 'A', name: '普通', tone: 'success', summary: '普通、可恢复：只改变「要不要问一次」。' },
  B: { label: 'B', name: '高风险', tone: 'warn', summary: '高风险：会削弱某类检测，必须逐项显式选择。' },
  C: { label: 'C', name: '不可修改', tone: 'muted', summary: 'HFK 永不写入，只做解释。' }
}

/** 服务端点（集中一处，避免散落的字符串）。 */
const API_PATHS = {
  health: '/api/health',
  env: '/api/env',
  status: '/api/status',
  catalogue: '/api/catalogue',
  presets: '/api/presets',
  transactions: '/api/transactions',
  backups: '/api/backups',
  config: '/api/config',
  preview: '/api/preview',
  apply: '/api/apply',
  restore: '/api/restore',
  uninstall: '/api/uninstall'
}

/** 事务状态 → 中文标签 + Badge 色调（对应 Python 的 STATUS_* 常量）。 */
const TRANSACTION_STATUS = {
  applied: { label: '待恢复', tone: 'warn' },
  restored: { label: '已恢复', tone: 'success' },
  partially_restored: { label: '部分恢复（有冲突）', tone: 'warn' },
  failed: { label: '失败（已回滚）', tone: 'destructive' }
}

// -----------------------------------------------------------------------------
// 1.1 基础判断与小工具
// -----------------------------------------------------------------------------

/** 永远返回数组（服务端字段缺失时 UI 不炸）。 */
function asArray(value) {
  return Array.isArray(value) ? value : []
}

/** 是否为普通对象（排除 null / 数组）。 */
function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

/** 安全取字符串；非字符串返回 ''。 */
function asText(value) {
  return typeof value === 'string' ? value : ''
}

/** 两位补零。 */
function pad2(value) {
  return String(value).padStart(2, '0')
}

/** 严格相等：true 与 1 是两种值（与 Python patch._same 的语义一致）。 */
function strictEqual(left, right) {
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false
    }
    for (let index = 0; index < left.length; index += 1) {
      if (!strictEqual(left[index], right[index])) {
        return false
      }
    }
    return true
  }
  if (typeof left !== typeof right) {
    return false
  }
  return left === right
}

/**
 * 人话格式化一个配置值。
 * @param {unknown} value
 * @param {boolean} exists 该键当前是否存在（false → 「未设置」）
 */
function fmtValue(value, exists) {
  if (exists === false) {
    return '（未设置）'
  }
  if (value === null || value === undefined) {
    return '（未设置）'
  }
  if (typeof value === 'boolean') {
    return value ? 'true' : 'false'
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return '（空列表）'
    }
    return value.map(item => String(item)).join(', ')
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch (error) {
      return '（无法显示）'
    }
  }
  return String(value)
}

/** 字节数 → 人类可读大小。 */
function fmtBytes(size) {
  const bytes = Number(size)
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '—'
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KiB`
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

/** ISO 时间 → 本地「YYYY-MM-DD HH:mm:ss」；解析失败原样返回。 */
function fmtTimestamp(input) {
  if (!input) {
    return '—'
  }
  const text = String(input)
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) {
    return text
  }
  const ymd = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
  const hms = `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`
  return `${ymd} ${hms}`
}

/** 拼接目录与相对路径，同时兼容 Windows 反斜杠与 POSIX 斜杠。 */
function joinPath(directory, relative) {
  const base = String(directory == null ? '' : directory).replace(/[\\/]+$/, '')
  const rest = String(relative == null ? '' : relative)
    .replace(/^[\\/]+/, '')
    .split(/[\\/]+/)
    .filter(Boolean)
    .join(base.includes('\\') ? '\\' : '/')
  return base ? `${base}${base.includes('\\') ? '\\' : '/'}${rest}` : rest
}

/** 按行拆分字符串列表（空行忽略）；也容忍逗号分隔的一行输入。 */
function parseListText(text) {
  if (typeof text !== 'string' || !text.trim()) {
    return []
  }
  return text
    .split(/[\n,]+/)
    .map(item => item.trim())
    .filter(Boolean)
}

/** 字符串列表 → 每行一项的编辑文本。 */
function formatListText(list) {
  return asArray(list)
    .map(item => String(item))
    .join('\n')
}

/** 稳定序列化「预设 + 覆盖」，用于判断某次 preview 是否仍对应当前选择。 */
function planSignature(preset, overrides) {
  const source = isPlainObject(overrides) ? overrides : {}
  const keys = Object.keys(source).sort()
  return JSON.stringify([String(preset || ''), keys.map(key => [key, source[key]])])
}

// -----------------------------------------------------------------------------
// 1.2 服务地址 / 服务发现文件解析
// -----------------------------------------------------------------------------

/** 只允许回环地址：HFK 服务按设计只监听 127.0.0.1，插件也拒绝连到别处。 */
function isLoopbackHostname(hostname) {
  const value = String(hostname || '').toLowerCase()
  return value === '127.0.0.1' || value === 'localhost' || value === '::1' || value === '[::1]'
}

/**
 * 规范化用户输入的服务地址。
 * @returns {{ok: true, baseUrl: string, port: number|null} | {ok: false, error: string}}
 */
function normalizeBaseUrl(raw) {
  let text = String(raw == null ? '' : raw).trim()
  if (!text) {
    return { ok: false, error: '服务地址为空' }
  }
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
    text = `http://${text}`
  }
  let url = null
  try {
    url = new URL(text)
  } catch (error) {
    return { ok: false, error: '服务地址格式不正确' }
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return { ok: false, error: '只支持 http:// 或 https:// 地址' }
  }
  if (!isLoopbackHostname(url.hostname)) {
    return { ok: false, error: '只允许连接本机回环地址（127.0.0.1 / localhost）' }
  }
  const port = url.port ? Number(url.port) : url.protocol === 'https:' ? 443 : 80
  const hostname = url.hostname.toLowerCase() === 'localhost' ? 'localhost' : url.hostname
  const pathname = url.pathname.replace(/\/+$/, '')
  const netloc = port === 80 || port === 443 ? hostname : `${hostname}:${port}`
  const schemeNetloc = port === 80 && url.protocol === 'http:' ? hostname : port === 443 && url.protocol === 'https:' ? hostname : netloc
  return { ok: true, baseUrl: `${url.protocol}//${schemeNetloc}${pathname}`, port: Number.isFinite(port) ? port : null }
}

/** 端口 → http://127.0.0.1:<port>；非法端口返回 null。 */
function baseUrlFromPort(port) {
  const value = Number(port)
  if (!Number.isInteger(value) || value < 1 || value > 65535) {
    return null
  }
  return `http://127.0.0.1:${value}`
}

/**
 * 解析 <HERMES_HOME>/hfk/service.json。
 * 期望字段：{ pid, port, base_url, token, hfk_version, started_at }
 * @returns {{ok: true, baseUrl: string, port: number|null, token: string, hfkVersion: string, startedAt: string, pid: number|null, error?: undefined} | {ok: false, error: string}}
 */
function parseServiceDescriptor(text) {
  if (typeof text !== 'string' || !text.trim()) {
    return { ok: false, error: 'service.json 内容为空' }
  }
  let data = null
  try {
    data = JSON.parse(text)
  } catch (error) {
    return { ok: false, error: 'service.json 不是合法 JSON' }
  }
  if (!isPlainObject(data)) {
    return { ok: false, error: 'service.json 结构不正确（顶层不是对象）' }
  }
  const token = asText(data.token).trim()
  if (!token) {
    return { ok: false, error: 'service.json 缺少 token 字段' }
  }
  let baseUrl = ''
  let port = Number.isFinite(Number(data.port)) ? Number(data.port) : null
  const declared = asText(data.base_url).trim()
  if (declared) {
    const normalized = normalizeBaseUrl(declared)
    if (normalized.ok) {
      baseUrl = normalized.baseUrl
      if (normalized.port) {
        port = normalized.port
      }
    }
  }
  if (!baseUrl) {
    const fromPort = baseUrlFromPort(port)
    if (!fromPort) {
      return { ok: false, error: 'service.json 缺少可用的 base_url / port' }
    }
    baseUrl = fromPort
  }
  const pid = Number.isFinite(Number(data.pid)) ? Number(data.pid) : null
  return {
    ok: true,
    baseUrl,
    port,
    token,
    hfkVersion: asText(data.hfk_version),
    startedAt: asText(data.started_at),
    pid
  }
}

// -----------------------------------------------------------------------------
// 1.3 API 错误与 HTTP 客户端
// -----------------------------------------------------------------------------

/** 构造一个带机器可读 code 的错误（不包含令牌、不包含请求头）。 */
function makeApiError(code, message, status, detail) {
  const error = new Error(message || '未知错误')
  error.name = 'HfkApiError'
  error.code = String(code || 'unknown')
  error.status = Number.isFinite(Number(status)) ? Number(status) : 0
  error.detail = detail ? String(detail) : ''
  return error
}

/**
 * 把任意错误翻译成中文「标题 + 建议 + 原始信息」。
 * 只读取 code/message/detail —— 调用方保证这些字段里没有令牌。
 */
function describeError(raw) {
  const error =
    raw && raw.name === 'HfkApiError'
      ? raw
      : makeApiError('unknown', raw && raw.message ? String(raw.message) : String(raw))
  const code = String(error.code || 'unknown')
  const table = {
    unreachable: [
      '无法连接 HFK Core 服务',
      '服务可能没有启动，或端口与发现文件不一致。请先在 HFK 项目目录启动服务，再点「重新连接」。'
    ],
    timeout: [
      'HFK Core 服务无响应',
      '请求超时：服务可能正忙或进程卡住。稍后重试；持续超时请重启服务。'
    ],
    unauthorized: [
      '令牌无效（HTTP 401）',
      'HFK 服务每次重启都会生成新令牌。请点「重新读取服务发现文件」；若仍失败，在高级设置中粘贴最新令牌。'
    ],
    bad_host: [
      '服务拒绝了请求来源（HTTP 403）',
      'HFK 只接受来自本机回环地址的请求。请确认服务地址是 127.0.0.1 / localhost。'
    ],
    unsupported_version: [
      'Hermes 版本未通过验证',
      'HFK 主动拒绝写入。当前版本只能查看历史与备份；换回已验证版本后重新预览。'
    ],
    signature_mismatch: [
      '配置文件在预览之后被修改',
      '为避免覆盖你刚做的改动，HFK 拒绝写入。请重新预览并确认最新差异。'
    ],
    config_read_error: [
      '配置文件不可读',
      'HFK 在配置读不出来时拒绝写入。请检查 config.yaml 是否存在、YAML 语法是否正确。'
    ],
    config_locked: [
      '配置文件被其他进程占用',
      'Windows 上常见于其他进程正持有 config.yaml。关闭相关进程后重试。'
    ],
    config_validation_error: [
      '补丁项不合法',
      '所选字段或取值没有通过校验，请按提示修正后重新预览。'
    ],
    config_write_error: [
      '写入失败，已回滚',
      'HFK 已用备份的原始字节回滚，配置应保持写入前状态；详情见下方 detail。'
    ],
    backup_error: [
      '备份失败，已中止',
      '没有通过校验的字节备份时，HFK 不会写入任何内容。'
    ],
    transaction_error: [
      '事务记录异常',
      '事务文件可能损坏或被占用；请检查 HFK 数据目录。'
    ],
    conflict_error: [
      '检测到后续修改',
      'HFK 不会覆盖你在补丁之后手动修改的值。'
    ],
    bad_body: [
      '请求体被拒绝',
      '插件发送的请求格式不符合服务预期；请更新插件或 HFK 版本。'
    ],
    internal: [
      'HFK 内部错误',
      '服务端出现未预期异常，详情见 HFK 日志（hfk/logs/hfk.log）。'
    ],
    parse_error: [
      '服务返回了无法解析的响应',
      '响应不是合法 JSON；该端口上可能跑着别的服务，请核对服务地址。'
    ],
    invalid_input: [
      '输入不合法',
      '请检查服务地址、令牌或字段取值。'
    ],
    no_service_file: [
      '没有找到服务发现文件',
      '请先启动 HFK Core 服务，它会写出 service.json。'
    ],
    unknown: [
      '操作失败',
      '请重试；若持续失败，请查看 HFK 日志与下方 detail。'
    ]
  }
  const entry = table[code] || table.unknown
  return {
    code,
    title: entry[0],
    hint: entry[1],
    message: asText(error.message),
    detail: asText(error.detail)
  }
}

/**
 * HFK Core HTTP 客户端。
 * @param {{baseUrl: string, token?: string, fetchImpl?: Function, timeoutMs?: number}} options
 */
function createClient(options) {
  const settings = options || {}
  const baseUrl = String(settings.baseUrl || '').replace(/\/+$/, '')
  const token = asText(settings.token)
  const fetchImpl = settings.fetchImpl || (typeof fetch === 'function' ? fetch : null)
  const defaultTimeoutMs = Number(settings.timeoutMs) > 0 ? Number(settings.timeoutMs) : 20000

  if (!baseUrl) {
    throw makeApiError('invalid_input', '缺少 HFK 服务地址')
  }
  if (typeof fetchImpl !== 'function') {
    throw makeApiError('invalid_input', '当前运行环境没有 fetch')
  }

  /**
   * @param {string} method
   * @param {string} path
   * @param {object|undefined} body
   * @param {{timeoutMs?: number, auth?: boolean}|undefined} requestOptions
   */
  async function request(method, path, body, requestOptions) {
    const optionsForRequest = requestOptions || {}
    const timeoutMs =
      Number(optionsForRequest.timeoutMs) > 0 ? Number(optionsForRequest.timeoutMs) : defaultTimeoutMs
    const headers = { Accept: 'application/json' }
    // /api/health 不需要令牌；其余请求必须带。令牌只在请求头里出现。
    if (token && optionsForRequest.auth !== false) {
      headers['X-HFK-Token'] = token
    }
    let bodyText
    if (body !== undefined && body !== null) {
      headers['Content-Type'] = 'application/json'
      bodyText = JSON.stringify(body)
    }
    const controller = typeof AbortController === 'function' ? new AbortController() : null
    let timer = null
    if (controller) {
      // 注意：这个 timer 在 finally 里必然清掉，不会跨请求泄漏（不是常驻轮询）。
      timer = setTimeout(() => {
        try {
          controller.abort()
        } catch (error) {
          /* 已经结束的请求，忽略 */
        }
      }, timeoutMs)
    }
    let response
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method,
        headers,
        body: bodyText,
        mode: 'cors',
        credentials: 'omit',
        cache: 'no-store',
        signal: controller ? controller.signal : undefined
      })
    } catch (error) {
      if (error && (error.name === 'AbortError' || error.code === 20)) {
        throw makeApiError('timeout', `请求 ${path} 超时（${timeoutMs} ms）`)
      }
      throw makeApiError(
        'unreachable',
        `无法连接 HFK Core 服务：${error && error.message ? error.message : '网络错误'}`,
        0
      )
    } finally {
      if (timer !== null) {
        clearTimeout(timer)
      }
    }

    let text = ''
    try {
      text = await response.text()
    } catch (error) {
      throw makeApiError('parse_error', `读取 ${path} 的响应失败`, response.status)
    }
    let data = null
    if (text.trim()) {
      try {
        data = JSON.parse(text)
      } catch (error) {
        if (response.ok) {
          throw makeApiError('parse_error', `${path} 返回了非 JSON 内容`, response.status)
        }
      }
    }
    if (!response.ok) {
      const serverError = data && isPlainObject(data.error) ? data.error : null
      const code = serverError && serverError.code ? String(serverError.code) : `http_${response.status}`
      const message =
        serverError && serverError.message ? String(serverError.message) : `服务返回 HTTP ${response.status}`
      throw makeApiError(
        code,
        message,
        response.status,
        serverError && serverError.detail ? String(serverError.detail) : ''
      )
    }
    return data
  }

  return {
    baseUrl,
    request,
    health: () => request('GET', API_PATHS.health, undefined, { timeoutMs: 3000, auth: false }),
    env: () => request('GET', API_PATHS.env),
    status: () => request('GET', API_PATHS.status),
    catalogue: () => request('GET', API_PATHS.catalogue),
    presets: () => request('GET', API_PATHS.presets),
    config: () => request('GET', API_PATHS.config),
    transactions: () => request('GET', API_PATHS.transactions),
    backups: () => request('GET', API_PATHS.backups),
    preview: body => request('POST', API_PATHS.preview, body || {}),
    apply: body => request('POST', API_PATHS.apply, body || {}, { timeoutMs: 30000 }),
    restore: body => request('POST', API_PATHS.restore, body || {}, { timeoutMs: 30000 }),
    uninstall: body => request('POST', API_PATHS.uninstall, body || {}, { timeoutMs: 30000 })
  }
}

// -----------------------------------------------------------------------------
// 1.4 配置投影 / 预设 / 选择集
// -----------------------------------------------------------------------------

/**
 * 把 GET /api/config 的「受管字段投影」变成 path → {exists, value}。
 * 只认 fields 字段；即使服务端（旧版本）多返回了 document，也**绝不**使用它。
 */
function configFieldMap(payload) {
  const map = {}
  if (!isPlainObject(payload) || payload.readable === false) {
    return map
  }
  if (!isPlainObject(payload.fields)) {
    return map
  }
  for (const path of Object.keys(payload.fields)) {
    const entry = payload.fields[path]
    if (!isPlainObject(entry)) {
      continue
    }
    map[path] = { exists: Boolean(entry.exists), value: entry.exists ? entry.value : null }
  }
  return map
}

/** 字段清单 → path → spec 映射。 */
function specsByPath(catalogue) {
  const map = {}
  for (const spec of asArray(catalogue && catalogue.fields)) {
    if (spec && typeof spec.path === 'string') {
      map[spec.path] = spec
    }
  }
  return map
}

/** 取预设的展示元信息（/api/presets 优先，兜底文案其次）。 */
function presetMeta(presetsPayload, key) {
  const items = presetsPayload && isPlainObject(presetsPayload.items) ? presetsPayload.items : {}
  const item = items[key]
  if (isPlainObject(item)) {
    return {
      label: asText(item.label) || key,
      description: asText(item.description),
      riskNote: asText(item.risk_note),
      requiresAcknowledgement: Boolean(item.requires_acknowledgement),
      changes: asArray(item.changes)
    }
  }
  const fallback = PRESET_META_FALLBACK[key] || {
    label: key,
    description: '',
    risk_note: '',
    requires_acknowledgement: false
  }
  return {
    label: fallback.label,
    description: fallback.description,
    riskNote: fallback.risk_note,
    requiresAcknowledgement: Boolean(fallback.requires_acknowledgement),
    changes: []
  }
}

/**
 * 某预设声明会改的字段。
 * 优先 /api/presets；它拿不到时用 /api/catalogue 里每个字段的 presets 映射还原。
 * @returns {Array<{path: string, title: string, value: unknown, risk: string, effect: string}>}
 */
function declaredPresetChanges(presetsPayload, catalogue, key) {
  const fromPresets = presetMeta(presetsPayload, key).changes
  if (fromPresets.length > 0) {
    return fromPresets.map(change => ({
      path: asText(change.path),
      title: asText(change.title) || asText(change.path),
      value: change.value,
      risk: asText(change.risk) || 'A',
      effect: asText(change.effect)
    }))
  }
  const rows = []
  for (const spec of asArray(catalogue && catalogue.fields)) {
    if (!spec || !isPlainObject(spec.presets)) {
      continue
    }
    if (!Object.prototype.hasOwnProperty.call(spec.presets, key)) {
      continue
    }
    rows.push({
      path: asText(spec.path),
      title: asText(spec.title) || asText(spec.path),
      value: spec.presets[key],
      risk: asText(spec.risk) || 'A',
      effect: asText(spec.effect)
    })
  }
  return rows
}

/**
 * 一个预设会真实改动多少项、前几项「当前 → 目标」。
 * 数据源优先级：
 *   1. 该预设的 /api/preview 结果（最权威：before/after/changed 都来自服务）；
 *   2. 预设声明 + /api/config 投影（离线兜底）。
 * @param {{changes?: unknown[], before?: object}|null} previewPayload
 * @param {Array} declaredChanges
 * @param {object} currentByPath
 * @param {object} specMap
 * @param {number} limit
 */
function presetChangeSummary(previewPayload, declaredChanges, currentByPath, specMap, limit) {
  const max = Number.isFinite(Number(limit)) && Number(limit) > 0 ? Number(limit) : 3
  const changed = []
  if (previewPayload && Array.isArray(previewPayload.changes)) {
    for (const change of previewPayload.changes) {
      if (!change || change.error || change.changed !== true) {
        continue
      }
      changed.push({
        path: asText(change.path),
        title: asText(change.title) || asText(change.path),
        risk: asText(change.risk) || 'A',
        from: fmtValue(change.before, change.before_exists !== false),
        to: fmtValue(change.after, true)
      })
    }
  } else {
    for (const change of asArray(declaredChanges)) {
      const path = asText(change.path)
      if (!path) {
        continue
      }
      const current = currentByPath[path] || { exists: false, value: null }
      const spec = specMap[path] || null
      const equalsDefault =
        !current.exists && spec && spec.default !== undefined && strictEqual(change.value, spec.default)
      if (current.exists && strictEqual(current.value, change.value)) {
        continue
      }
      if (equalsDefault) {
        continue
      }
      changed.push({
        path,
        title: asText(change.title) || path,
        risk: asText(change.risk) || 'A',
        from: fmtValue(current.value, current.exists),
        to: fmtValue(change.value, true)
      })
    }
  }
  return { count: changed.length, items: changed.slice(0, max) }
}

/** 该键当前值是否与目标值不同（供逐项勾选时即时反馈）。 */
function selectionChangesCurrent(spec, value, currentByPath) {
  if (!spec) {
    return false
  }
  const current = currentByPath[spec.path] || { exists: false, value: null }
  if (!current.exists) {
    return !strictEqual(value, spec.default)
  }
  return !strictEqual(current.value, value)
}

/**
 * 根据预设 + 已声明的变更 + 当前配置，构造逐项选择状态。
 * @returns {Object<string, {enabled: boolean, value: unknown, fromPreset: boolean}>}
 */
function buildSelection(presetKey, declaredChanges, catalogue, currentByPath) {
  const presetValues = {}
  for (const change of asArray(declaredChanges)) {
    const path = asText(change.path)
    if (path) {
      presetValues[path] = change.value
    }
  }
  const selection = {}
  for (const spec of asArray(catalogue && catalogue.fields)) {
    const path = asText(spec.path)
    if (!path) {
      continue
    }
    const fromPreset = Object.prototype.hasOwnProperty.call(presetValues, path)
    const current = currentByPath[path] || { exists: false, value: null }
    let baseValue = null
    if (current.exists) {
      baseValue = current.value
    } else if (spec.default !== undefined) {
      baseValue = spec.default
    }
    selection[path] = {
      enabled: Boolean(fromPreset),
      value: fromPreset ? presetValues[path] : baseValue,
      fromPreset
    }
  }
  return selection
}

/** 按字段类型把编辑器里的值规整成服务端会接受的值。 */
function coerceValue(kind, value) {
  if (kind === 'bool') {
    return Boolean(value)
  }
  if (kind === 'int') {
    if (typeof value === 'number') {
      return value
    }
    const parsed = Number.parseInt(String(value), 10)
    return Number.isNaN(parsed) ? value : parsed
  }
  if (kind === 'list') {
    if (Array.isArray(value)) {
      return value.map(item => String(item))
    }
    return parseListText(String(value == null ? '' : value))
  }
  if (kind === 'enum' || kind === 'str') {
    return String(value == null ? '' : value)
  }
  return value
}

/**
 * 逐项选择 → POST /api/preview / /api/apply 的 overrides。
 *   · 已勾选 → 写入实际值
 *   · 未勾选且该字段来自预设 → 写 null（= 从预设中剔除）
 *   · 未勾选且不来自预设 → 不出现（= 不修改）
 */
function buildOverrides(catalogue, selection) {
  const overrides = {}
  for (const spec of asArray(catalogue && catalogue.fields)) {
    const path = asText(spec.path)
    const row = selection && selection[path]
    if (!path || !row) {
      continue
    }
    if (row.enabled) {
      overrides[path] = coerceValue(asText(spec.kind), row.value)
    } else if (row.fromPreset) {
      overrides[path] = null
    }
  }
  return overrides
}

/** 客户端侧字段校验（与 hfk/schema.py:validate_value 保持一致）。 */
function validateFieldValue(spec, value) {
  const errors = []
  if (!spec) {
    return ['字段不在已验证字段清单内']
  }
  const path = asText(spec.path)
  const kind = asText(spec.kind)
  if (kind === 'enum') {
    const values = asArray(spec.values)
    if (typeof value !== 'string' || !values.includes(value)) {
      errors.push(`${path} 只接受 ${values.join(' / ')}，收到 ${fmtValue(value, true)}`)
    }
  } else if (kind === 'bool') {
    if (typeof value !== 'boolean') {
      errors.push(`${path} 只接受布尔值，收到 ${fmtValue(value, true)}`)
    }
  } else if (kind === 'int') {
    if (typeof value !== 'number' || !Number.isInteger(value)) {
      errors.push(`${path} 只接受整数，收到 ${fmtValue(value, true)}`)
    } else {
      if (spec.minimum !== null && spec.minimum !== undefined && value < spec.minimum) {
        errors.push(`${path} 不得小于 ${spec.minimum}，收到 ${value}`)
      }
      if (spec.maximum !== null && spec.maximum !== undefined && value > spec.maximum) {
        errors.push(`${path} 不得大于 ${spec.maximum}，收到 ${value}`)
      }
    }
  } else if (kind === 'str') {
    if (typeof value !== 'string') {
      errors.push(`${path} 只接受字符串`)
    }
  } else if (kind === 'list') {
    if (!Array.isArray(value) || !value.every(item => typeof item === 'string')) {
      errors.push(`${path} 只接受字符串列表`)
    }
  }
  return errors
}

/** 收集当前选择里所有校验错误（勾选项才校验）。 */
function collectSelectionErrors(catalogue, selection) {
  const errors = []
  for (const spec of asArray(catalogue && catalogue.fields)) {
    const path = asText(spec.path)
    const row = selection && selection[path]
    if (!row || !row.enabled) {
      continue
    }
    for (const message of validateFieldValue(spec, coerceValue(asText(spec.kind), row.value))) {
      errors.push(message)
    }
  }
  return errors
}

/** 预览结果摘要（用于「补丁管理」区块）。 */
function previewSummary(payload) {
  const changes = asArray(payload && payload.changes)
  let changed = 0
  let unchanged = 0
  let failed = 0
  for (const change of changes) {
    if (!change) {
      continue
    }
    if (change.error) {
      failed += 1
    } else if (change.changed === true) {
      changed += 1
    } else {
      unchanged += 1
    }
  }
  return {
    changed,
    unchanged,
    failed,
    hasEffect: Boolean(payload && payload.has_effect),
    errors: asArray(payload && payload.errors),
    warnings: asArray(payload && payload.warnings)
  }
}

/** 事务状态展示信息。 */
function transactionMeta(status) {
  const key = asText(status)
  const entry = TRANSACTION_STATUS[key]
  if (entry) {
    return entry
  }
  return { label: key || '未知', tone: 'muted' }
}

/**
 * 推断「config.yaml 里**当前实际生效**的预设」。
 *
 * 为什么不靠 UI 记忆：用户应用完补丁后切到聊天再回来，组件会重新挂载；
 * 如果只靠 useState/选中态，界面会退回默认档，让人误以为配置被还原了。
 * 正确的判据永远在磁盘上：某预设声明的**每一项**当前值都等于其目标值。
 *
 * @returns {string|null} 预设 key；与任何预设都不完全一致时返回 null
 */
function detectActivePreset(presetsPayload, catalogue, currentByPath) {
  const fields = asArray(catalogue && catalogue.fields)
  if (!fields.length || !isPlainObject(currentByPath)) {
    return null
  }
  let best = null
  for (const key of PRESET_KEYS) {
    if (key === 'custom') {
      continue
    }
    const declared = declaredPresetChanges(presetsPayload, catalogue, key)
    if (!declared.length) {
      continue
    }
    const allMatch = declared.every(change => {
      const path = asText(change.path)
      const current = currentByPath[path]
      if (!current) {
        return false
      }
      return strictEqual(current.value, change.value)
    })
    // 多个预设同时满足时，取「声明项更多」的那个（更具体的匹配）
    if (allMatch && (!best || declared.length > best.count)) {
      best = { key, count: declared.length }
    }
  }
  return best ? best.key : null
}

/**
 * 当前选择集是否「就是预设原样」——没有任何用户改写。
 *
 * 用途：判断风险确认还需要不需要。若预设已生效且选择集没有被改动，
 * 那这次「应用」不会写入任何东西，再要求确认风险只是噪音。
 */
function selectionMatchesDeclared(selection, declared) {
  const declaredMap = new Map()
  for (const change of asArray(declared)) {
    const path = asText(change.path)
    if (path) {
      declaredMap.set(path, change.value)
    }
  }
  for (const [path, value] of declaredMap) {
    const row = selection ? selection[path] : null
    if (!row || row.enabled !== true || !strictEqual(row.value, value)) {
      return false
    }
  }
  for (const path of Object.keys(selection || {})) {
    const row = selection[path]
    if (row && row.enabled && !declaredMap.has(path)) {
      return false // 用户额外开了预设之外的字段
    }
  }
  return declaredMap.size > 0
}

// #endregion hfk-pure-core
// =============================================================================
// 2) UI 设计令牌与小型展示组件
// -----------------------------------------------------------------------------
// 颜色一律走主题变量（--ui-*），这样浅色/深色主题自动适配；不写死任何颜色。
// 因为 disk 插件的字符串不会被 Tailwind 编译，这里用 inline style 保证生效，
// 交互控件则使用 App 自己的 UI kit（Button/Badge/Switch/Checkbox/Select…）。
// =============================================================================

const CSS = {
  text: 'var(--ui-text-primary)',
  textSecondary: 'var(--ui-text-secondary)',
  textTertiary: 'var(--ui-text-tertiary)',
  textQuaternary: 'var(--ui-text-quaternary)',
  border: 'var(--ui-stroke-secondary)',
  borderSoft: 'var(--ui-stroke-tertiary)',
  bgPrimary: 'var(--ui-bg-primary)',
  bgSecondary: 'var(--ui-bg-secondary)',
  bgQuaternary: 'var(--ui-bg-quaternary)',
  bgEditor: 'var(--ui-bg-editor)',
  accent: 'var(--ui-accent)',
  green: 'var(--ui-green)',
  red: 'var(--ui-red)',
  orange: 'var(--ui-orange)',
  yellow: 'var(--ui-yellow)',
  blue: 'var(--ui-blue)',
  mono: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
}

const FS = {
  body: '0.8125rem',
  small: '0.75rem',
  caption: '0.6875rem',
  title: '1.0625rem'
}

const STYLES = {
  page: {
    height: '100%',
    minHeight: 0,
    overflowY: 'auto',
    boxSizing: 'border-box',
    padding: '18px 24px 64px',
    color: CSS.text,
    fontSize: FS.body,
    lineHeight: 1.55
  },
  header: {
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    gap: '12px',
    flexWrap: 'wrap',
    marginBottom: '14px'
  },
  h1: { margin: 0, fontSize: FS.title, fontWeight: 600, letterSpacing: '0.01em' },
  subtitle: { margin: '2px 0 0', color: CSS.textTertiary, fontSize: FS.small },
  section: { marginTop: '22px' },
  sectionHead: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    marginBottom: '10px',
    flexWrap: 'wrap'
  },
  sectionTitle: { margin: 0, fontSize: FS.body, fontWeight: 600 },
  sectionHint: { color: CSS.textTertiary, fontSize: FS.caption, margin: 0 },
  card: {
    border: `1px solid ${CSS.borderSoft}`,
    borderRadius: '6px',
    background: CSS.bgPrimary,
    padding: '10px 12px'
  },
  row: {
    borderTop: `1px solid ${CSS.borderSoft}`,
    padding: '10px 0',
    display: 'flex',
    gap: '12px',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    flexWrap: 'wrap'
  },
  label: { fontWeight: 600, fontSize: FS.body, color: CSS.text },
  caption: { color: CSS.textTertiary, fontSize: FS.caption },
  mono: {
    fontFamily: CSS.mono,
    fontSize: FS.caption,
    color: CSS.textSecondary,
    wordBreak: 'break-all'
  },
  code: {
    fontFamily: CSS.mono,
    fontSize: FS.caption,
    background: CSS.bgQuaternary,
    border: `1px solid ${CSS.borderSoft}`,
    borderRadius: '4px',
    padding: '2px 6px',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-all'
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
    gap: '10px'
  },
  statGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(168px, 1fr))',
    gap: '8px'
  },
  stat: {
    border: `1px solid ${CSS.borderSoft}`,
    borderRadius: '6px',
    padding: '8px 10px',
    background: CSS.bgPrimary,
    minWidth: 0
  },
  statLabel: { color: CSS.textTertiary, fontSize: FS.caption, marginBottom: '2px' },
  statValue: {
    color: CSS.text,
    fontSize: FS.body,
    fontWeight: 600,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap'
  },
  statDetail: { color: CSS.textTertiary, fontSize: FS.caption, marginTop: '2px' },
  inline: { display: 'inline-flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' },
  actions: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' },
  spacer: { flex: 1 },
  badgeRow: { display: 'inline-flex', alignItems: 'center', gap: '4px', flexWrap: 'wrap' },
  notice: {
    borderRadius: '6px',
    borderWidth: '1px',
    borderStyle: 'solid',
    padding: '8px 10px',
    marginBottom: '12px',
    display: 'flex',
    alignItems: 'flex-start',
    gap: '8px'
  },
  bannerList: { margin: '4px 0 0', paddingLeft: '18px', color: CSS.textSecondary, fontSize: FS.small },
  empty: { padding: '14px 0', color: CSS.textTertiary, fontSize: FS.small },
  ruler: { border: 'none', borderTop: `1px solid ${CSS.borderSoft}`, margin: '10px 0' },
  details: { color: CSS.textTertiary, fontSize: FS.caption, marginTop: '4px' },
  summary: { cursor: 'pointer', userSelect: 'none' },
  inputRow: { display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' },
  presetCard: {
    border: `1px solid ${CSS.borderSoft}`,
    borderRadius: '8px',
    padding: '12px',
    background: CSS.bgPrimary,
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
    cursor: 'pointer',
    minWidth: 0
  },
  presetCardActive: {
    border: `1px solid ${CSS.accent}`,
    boxShadow: 'inset 0 0 0 1px var(--ui-accent)'
  },
  presetTitleRow: { display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' },
  presetDesc: { margin: 0, color: CSS.textSecondary, fontSize: FS.small, minHeight: '2.6em' },
  presetChange: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '8px',
    fontSize: FS.caption,
    color: CSS.textSecondary
  },
  presetChangePath: { fontFamily: CSS.mono, color: CSS.textTertiary, overflow: 'hidden', textOverflow: 'ellipsis' },
  callout: {
    borderRadius: '6px',
    border: `1px solid color-mix(in srgb, ${CSS.red} 35%, transparent)`,
    background: `color-mix(in srgb, ${CSS.red} 9%, transparent)`,
    padding: '8px 10px',
    color: CSS.text,
    fontSize: FS.small
  },
  calloutWarn: {
    borderRadius: '6px',
    border: `1px solid color-mix(in srgb, ${CSS.orange} 35%, transparent)`,
    background: `color-mix(in srgb, ${CSS.orange} 9%, transparent)`,
    padding: '8px 10px',
    color: CSS.text,
    fontSize: FS.small
  },
  calloutInfo: {
    borderRadius: '6px',
    border: `1px solid ${CSS.borderSoft}`,
    background: CSS.bgSecondary,
    padding: '8px 10px',
    color: CSS.textSecondary,
    fontSize: FS.small
  },
  fieldRow: {
    borderTop: `1px solid ${CSS.borderSoft}`,
    padding: '10px 0',
    display: 'grid',
    gridTemplateColumns: 'minmax(0, 1fr) auto',
    gap: '10px 14px',
    alignItems: 'start'
  },
  fieldMain: { minWidth: 0, display: 'flex', flexDirection: 'column', gap: '3px' },
  fieldTitleRow: { display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' },
  fieldEditor: { display: 'flex', flexDirection: 'column', gap: '4px', alignItems: 'flex-end' },
  fieldDiff: { fontSize: FS.small, color: CSS.textSecondary },
  fieldEffect: { fontSize: FS.small, color: CSS.textTertiary, margin: 0 },
  blockedRow: {
    borderTop: `1px solid ${CSS.borderSoft}`,
    padding: '8px 0',
    display: 'flex',
    flexDirection: 'column',
    gap: '2px'
  },
  floorRow: {
    borderTop: `1px solid ${CSS.borderSoft}`,
    padding: '8px 0',
    display: 'flex',
    flexDirection: 'column',
    gap: '2px'
  },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: FS.small },
  th: {
    textAlign: 'left',
    color: CSS.textTertiary,
    fontWeight: 500,
    fontSize: FS.caption,
    padding: '4px 8px 4px 0',
    borderBottom: `1px solid ${CSS.borderSoft}`
  },
  td: {
    color: CSS.textSecondary,
    padding: '5px 8px 5px 0',
    borderBottom: `1px solid ${CSS.borderSoft}`,
    verticalAlign: 'top'
  },
  tabPanel: { marginTop: '10px' },
  errorText: { color: CSS.red, fontSize: FS.small },
  successText: { color: CSS.green, fontSize: FS.small },
  mutedText: { color: CSS.textTertiary, fontSize: FS.small },
  monoStrong: { fontFamily: CSS.mono, fontSize: FS.small, color: CSS.text },
  list: { margin: '4px 0 0', paddingLeft: '18px', color: CSS.textSecondary, fontSize: FS.small },
  centerLoading: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: '8px',
    padding: '56px 16px',
    color: CSS.textSecondary,
    fontSize: FS.body
  },
  connectionForm: { display: 'grid', gap: '8px', maxWidth: '560px' },
  connectionLabel: { fontSize: FS.caption, color: CSS.textTertiary },
  linkButton: {
    background: 'none',
    border: 'none',
    padding: 0,
    color: CSS.accent,
    cursor: 'pointer',
    font: 'inherit',
    fontSize: FS.small,
    textDecoration: 'underline'
  }
}

/** 小标题 + 右侧操作位的分区容器。 */
function Section(props) {
  return jsxs('section', {
    style: STYLES.section,
    children: [
      jsxs('div', {
        style: STYLES.sectionHead,
        children: [
          jsx('h2', { style: STYLES.sectionTitle, children: props.title }),
          props.badge ? props.badge : null,
          jsx('div', { style: STYLES.spacer }),
          props.aside || null
        ]
      }),
      props.hint ? jsx('p', { style: STYLES.sectionHint, children: props.hint }) : null,
      jsx('div', { children: props.children })
    ]
  })
}

/** 顶部状态条里的一格。 */
function StatTile(props) {
  return jsxs('div', {
    style: STYLES.stat,
    children: [
      jsx('div', { style: STYLES.statLabel, children: props.label }),
      jsxs('div', {
        style: { ...STYLES.statValue, color: props.tone || CSS.text },
        children: [props.value]
      }),
      props.detail ? jsx('div', { style: STYLES.statDetail, children: props.detail }) : null
    ]
  })
}

/** 通用提示条（success / warn / error / info）。 */
function Notice(props) {
  const kind = props.kind || 'info'
  const palette = {
    success: { border: `color-mix(in srgb, ${CSS.green} 40%, transparent)`, bg: `color-mix(in srgb, ${CSS.green} 10%, transparent)`, icon: 'pass', tone: CSS.green },
    warn: { border: `color-mix(in srgb, ${CSS.orange} 40%, transparent)`, bg: `color-mix(in srgb, ${CSS.orange} 10%, transparent)`, icon: 'warning', tone: CSS.orange },
    error: { border: `color-mix(in srgb, ${CSS.red} 40%, transparent)`, bg: `color-mix(in srgb, ${CSS.red} 10%, transparent)`, icon: 'error', tone: CSS.red },
    info: { border: CSS.borderSoft, bg: CSS.bgSecondary, icon: 'info', tone: CSS.textSecondary }
  }
  const colors = palette[kind] || palette.info
  return jsxs('div', {
    style: { ...STYLES.notice, borderColor: colors.border, background: colors.bg },
    role: kind === 'error' ? 'alert' : 'status',
    children: [
      jsx(Codicon, { name: colors.icon, style: { color: colors.tone, marginTop: '2px' } }),
      jsxs('div', {
        style: { minWidth: 0, flex: 1 },
        children: [
          props.title ? jsx('div', { style: { fontWeight: 600, fontSize: FS.small }, children: props.title }) : null,
          props.message ? jsx('div', { style: { fontSize: FS.small, color: CSS.textSecondary, whiteSpace: 'pre-wrap' }, children: props.message }) : null,
          props.detail ? jsx('div', { style: { fontSize: FS.caption, color: CSS.textTertiary, marginTop: '2px', whiteSpace: 'pre-wrap' }, children: props.detail }) : null,
          props.children || null
        ]
      }),
      props.onDismiss
        ? jsx(Button, {
            variant: 'ghost',
            size: 'icon-xs',
            'aria-label': '关闭',
            onClick: props.onDismiss,
            children: jsx(Codicon, { name: 'close' })
          })
        : null
    ]
  })
}

/** 风险徽章：A 绿 / B 橙 / C 灰。 */
function RiskBadge(props) {
  const meta = RISK_META[props.risk] || RISK_META.C
  return jsx(Badge, { variant: meta.tone, title: meta.summary, children: `${meta.label} · ${meta.name}` })
}

/** 一行「标题 + 值」的紧凑展示，用于历史/状态区。 */
function KeyValue(props) {
  return jsxs('div', {
    style: { display: 'flex', gap: '8px', fontSize: FS.small, color: CSS.textSecondary, alignItems: 'baseline', flexWrap: 'wrap' },
    children: [
      jsx('span', { style: { color: CSS.textTertiary, minWidth: '84px' }, children: props.label }),
      jsx('span', { style: props.mono ? STYLES.monoStrong : { color: CSS.textSecondary }, children: props.value })
    ]
  })
}

/** 可复制的命令/路径块（不提供「复制」按钮，避免和宿主剪贴板权限纠缠）。 */
function CodeLine(props) {
  return jsx('div', { style: STYLES.code, children: props.children })
}

// =============================================================================
// 3) 连接发现与数据加载
// -----------------------------------------------------------------------------
// 令牌来源（按优先级）：
//   1. 用户在「高级设置 / 连接设置」里手动粘贴的地址 + 令牌（仅本次会话，不落盘）；
//   2. <HERMES_HOME>\hfk\service.json（服务每次启动写入，权限 0600）。
// HERMES_HOME 优先取 host.status().hermes_home，取不到时用内置默认值。
// 令牌只保存在内存 state 里，绝不渲染、绝不持久化、绝不写日志。
// =============================================================================

/** 全局共享的「手动连接覆盖」。设置页与主页面都订阅它。
 *  token 只存在于内存（hot reload / 页面重启后需要重新粘贴）。 */
const overridesAtom = atom({ baseUrl: '', token: '' })

/** 从 ctx.storage 读取已保存的地址覆盖（令牌不持久化）。 */
function loadStoredOverrides(storage) {
  try {
    const stored = storage && typeof storage.get === 'function' ? storage.get('connection', null) : null
    const baseUrl = isPlainObject(stored) && typeof stored.baseUrl === 'string' ? stored.baseUrl : ''
    return { baseUrl, token: '' }
  } catch (error) {
    return { baseUrl: '', token: '' }
  }
}

/** 只持久化地址覆盖；令牌按设计不入盘。 */
function persistBaseUrl(storage, baseUrl) {
  try {
    if (storage && typeof storage.set === 'function') {
      storage.set('connection', { baseUrl: asText(baseUrl) })
    }
  } catch (error) {
    /* 存储失败不影响本次会话 */
  }
}

/** 记住用户上次选的预设档，回来时不再跳回「平衡」。 */
function loadStoredPreset(storage) {
  try {
    const stored = storage && typeof storage.get === 'function' ? storage.get('preset', null) : null
    const key = isPlainObject(stored) ? asText(stored.key) : asText(stored)
    return PRESET_KEYS.includes(key) ? key : ''
  } catch (error) {
    return ''
  }
}

/** 保存当前选中的预设档。 */
function persistPreset(storage, key) {
  try {
    if (storage && typeof storage.set === 'function' && PRESET_KEYS.includes(asText(key))) {
      storage.set('preset', { key: asText(key) })
    }
  } catch (error) {
    /* 存储失败不影响本次会话 */
  }
}

/** 给 Promise 加一个上限，避免 host.status() 之类的宿主调用把启动流程卡死。 */
function withTimeout(promise, timeoutMs, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(makeApiError('timeout', `${label || '宿主调用'} 超时（${timeoutMs} ms）`))
    }, timeoutMs)
    Promise.resolve(promise).then(
      value => {
        clearTimeout(timer)
        resolve(value)
      },
      error => {
        clearTimeout(timer)
        reject(error)
      }
    )
  })
}

/** 解析当前窗口的 HERMES_HOME（优先宿主给的值，其次内置默认值）。 */
async function resolveHermesHome() {
  try {
    const status = await withTimeout(host.status(), 2500, 'host.status()')
    const home = status && typeof status.hermes_home === 'string' ? status.hermes_home.trim() : ''
    if (home) {
      return { home, source: 'host.status().hermes_home' }
    }
  } catch (error) {
    /* 宿主还没就绪：退回默认值，页面会显示实际尝试过的路径 */
  }
  return { home: DEFAULT_HERMES_HOME, source: '内置默认值（host.status() 不可用）' }
}

/**
 * 通过 Electron 桥读取一个本地文本文件。
 * readFileText 对「文件不存在」返回 {ok:false,...} 而不是 reject，两种都要处理。
 */
async function readTextFile(path) {
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop : null
  if (!bridge || typeof bridge.readFileText !== 'function') {
    return { ok: false, status: 'no-bridge', message: '当前桌面版本没有提供文件读取桥接（window.hermesDesktop.readFileText）' }
  }
  try {
    const result = await bridge.readFileText(path)
    if (!result || typeof result !== 'object') {
      return { ok: false, status: 'error', message: '读取返回了空结果' }
    }
    if (result.ok === false) {
      return { ok: false, status: 'missing', message: asText(result.message) || asText(result.error) || '文件不存在或不可读' }
    }
    if (typeof result.text !== 'string') {
      return { ok: false, status: 'error', message: '读取结果不含文本内容' }
    }
    return { ok: true, status: 'ok', text: result.text, path: asText(result.path) || path }
  } catch (error) {
    return { ok: false, status: 'error', message: String(error && error.message ? error.message : error) }
  }
}

/**
 * 依次在候选 HERMES_HOME 下查找 service.json。
 * @returns {Promise<{home: string, homeSource: string, paths: Array<{home: string, path: string, status: string, message: string}>, descriptor: object|null}>}
 */
async function locateServiceFile() {
  const homeInfo = await resolveHermesHome()
  const homes = []
  for (const candidate of [homeInfo.home, DEFAULT_HERMES_HOME]) {
    const text = asText(candidate).trim()
    if (text && !homes.includes(text)) {
      homes.push(text)
    }
  }
  const attempts = []
  for (const home of homes) {
    const path = joinPath(home, SERVICE_FILE_RELATIVE)
    const read = await readTextFile(path)
    if (read.ok) {
      const descriptor = parseServiceDescriptor(read.text)
      attempts.push({
        home,
        path,
        status: descriptor.ok ? 'ok' : 'invalid',
        message: descriptor.ok ? '' : descriptor.error
      })
      if (descriptor.ok) {
        return { home: homeInfo.home, homeSource: homeInfo.source, paths: attempts, descriptor }
      }
      continue
    }
    attempts.push({ home, path, status: read.status, message: read.message })
  }
  return { home: homeInfo.home, homeSource: homeInfo.source, paths: attempts, descriptor: null }
}

/**
 * 建立到 HFK Core 的连接：先确定候选地址，再用 /api/health 探测。
 * 任何失败都返回可展示的状态，绝不抛出到 React 之外。
 */
async function connectToService(overrides) {
  const overrideState = overrides || {}
  const discovery = await locateServiceFile()
  const candidates = []
  const manuallyEntered = asText(overrideState.baseUrl).trim()

  let overrideBase = null
  if (manuallyEntered) {
    overrideBase = normalizeBaseUrl(manuallyEntered)
    if (!overrideBase.ok) {
      return {
        phase: 'down',
        conn: null,
        discovery,
        health: null,
        error: makeApiError('invalid_input', `手动填写的服务地址不合法：${overrideBase.error}`)
      }
    }
  } else if (asText(overrideState.token).trim()) {
    // 只有令牌、没有地址：必须靠发现文件拿端口；发现文件缺失时无法猜端口。
    if (!discovery.descriptor) {
      return {
        phase: 'down',
        conn: null,
        discovery,
        health: null,
        error: makeApiError('no_service_file', '只填写了令牌但没有服务地址，且没有找到 service.json，无法确定端口')
      }
    }
  }

  if (overrideBase && overrideBase.ok) {
    const token = asText(overrideState.token).trim() || (discovery.descriptor ? discovery.descriptor.token : '')
    candidates.push({
      baseUrl: overrideBase.baseUrl,
      token,
      source: asText(overrideState.token).trim() ? '手动覆盖（地址 + 令牌）' : '手动覆盖地址 + 发现文件令牌'
    })
  }
  if (discovery.descriptor) {
    const alreadyAdded = candidates.some(candidate => candidate.baseUrl === discovery.descriptor.baseUrl)
    if (!alreadyAdded) {
      candidates.push({
        baseUrl: discovery.descriptor.baseUrl,
        token: discovery.descriptor.token,
        source: 'service.json 自动发现'
      })
    }
  }

  if (candidates.length === 0) {
    return {
      phase: 'down',
      conn: null,
      discovery,
      health: null,
      error: makeApiError('no_service_file', '没有找到 service.json，也没有可用的服务地址')
    }
  }

  let lastError = null
  for (const candidate of candidates) {
    let client = null
    try {
      client = createClient({ baseUrl: candidate.baseUrl, token: candidate.token })
    } catch (error) {
      lastError = error
      continue
    }
    try {
      const health = await client.health()
      if (!health || health.ok !== true) {
        lastError = makeApiError('unreachable', `健康检查未通过：${candidate.baseUrl}`)
        continue
      }
      return {
        phase: 'ready',
        conn: {
          baseUrl: candidate.baseUrl,
          token: candidate.token,
          source: candidate.source,
          client,
          hfkVersion: asText(health.hfk_version)
        },
        discovery,
        health,
        error: null
      }
    } catch (error) {
      lastError = error
    }
  }
  return {
    phase: 'down',
    conn: null,
    discovery,
    health: null,
    error: lastError || makeApiError('unreachable', '无法连接 HFK Core 服务')
  }
}

/** 一次拉取全部只读数据；单个端点失败只影响对应区块。 */
async function loadStore(client) {
  const endpoints = [
    ['env', client.env],
    ['status', client.status],
    ['catalogue', client.catalogue],
    ['presets', client.presets],
    ['config', client.config],
    ['transactions', client.transactions],
    ['backups', client.backups]
  ]
  const results = await Promise.allSettled(endpoints.map(entry => entry[1]()))
  const data = {
    env: null,
    status: null,
    catalogue: null,
    presets: null,
    config: null,
    transactions: null,
    backups: null
  }
  const errors = []
  results.forEach((result, index) => {
    const key = endpoints[index][0]
    if (result.status === 'fulfilled' && result.value !== null && result.value !== undefined) {
      data[key] = result.value
      return
    }
    errors.push({
      key,
      error:
        result.status === 'rejected'
          ? result.reason
          : makeApiError('parse_error', `${key} 返回了空响应`)
    })
  })
  const unauthorized = errors.some(item => item.error && String(item.error.code) === 'unauthorized')
  return { data, errors, unauthorized }
}

/** 为四个预设各跑一次只读预览，用于卡片上的「将改动 N 项 / 当前 → 目标」。 */
async function loadPresetPreviews(client) {
  const keys = PRESET_KEYS.filter(key => key !== 'custom')
  const entries = await Promise.all(
    keys.map(async key => {
      try {
        const payload = await client.preview({ preset: key, overrides: {} })
        return [key, payload]
      } catch (error) {
        return [key, null]
      }
    })
  )
  const previews = {}
  for (const entry of entries) {
    if (entry[1]) {
      previews[entry[0]] = entry[1]
    }
  }
  return previews
}

/** 兼容性结论（env.compat 为准，status 兜底）。 */
function compatState(env, status) {
  const compat = env && isPlainObject(env.compat) ? env.compat : null
  let supported = null
  if (compat && typeof compat.supported === 'boolean') {
    supported = compat.supported
  } else if (status && typeof status.supported === 'boolean') {
    supported = status.supported
  }
  const exact = compat && typeof compat.exact === 'boolean' ? compat.exact : null
  const reason = asText(compat && compat.reason) || asText(status && status.compat_reason)
  if (supported === null) {
    return { supported: null, exact: null, reason, label: '未知', tone: CSS.textSecondary }
  }
  if (supported && exact === true) {
    return { supported: true, exact: true, reason, label: '精确支持', tone: CSS.green }
  }
  if (supported) {
    return { supported: true, exact: false, reason, label: '同族支持（需重新预览）', tone: CSS.orange }
  }
  return { supported: false, exact: false, reason, label: '不支持（只读）', tone: CSS.red }
}

/** Gateway 状态摘要。 */
function gatewayState(env) {
  const gateway = env && isPlainObject(env.gateway) ? env.gateway : null
  if (!gateway) {
    return { value: '未知', detail: '环境报告里没有 gateway 字段', tone: CSS.textSecondary }
  }
  if (gateway.running) {
    const detailParts = []
    if (gateway.state) {
      detailParts.push(`状态 ${gateway.state}`)
    }
    if (gateway.active_agents !== null && gateway.active_agents !== undefined) {
      detailParts.push(`活跃 Agent ${gateway.active_agents}`)
    }
    return {
      value: `运行中${gateway.pid ? ` · pid ${gateway.pid}` : ''}`,
      detail: detailParts.join(' · ') || 'Gateway 已就绪',
      tone: CSS.green
    }
  }
  return { value: '未运行', detail: asText(gateway.detail) || '没有发现正在运行的 Gateway', tone: CSS.orange }
}

/** 补丁状态摘要。 */
function patchState(status, activeCount) {
  const pending = Number(status && status.pending_transactions) || 0
  const total = Number(status && status.total_transactions) || 0
  if (pending > 0) {
    return { value: `${pending} 个事务待恢复`, detail: '建议先在「事务历史」确认，再决定是否恢复', tone: CSS.orange }
  }
  if (activeCount > 0) {
    return { value: `已应用 · ${activeCount} 项生效`, detail: '没有待恢复事务', tone: CSS.green }
  }
  if (total > 0) {
    return { value: '未应用', detail: `历史事务 ${total} 个`, tone: CSS.textSecondary }
  }
  return { value: '未应用', detail: '还没有任何 HFK 事务', tone: CSS.textSecondary }
}

/** 服务发现文件读取结果的展示文案。 */
function discoveryStatusLabel(status) {
  const map = {
    ok: '已读取',
    invalid: '内容无效',
    missing: '不存在',
    error: '读取失败',
    'no-bridge': '无文件读取桥接'
  }
  return map[status] || '未知'
}

/** 顶部状态区：标题 + 六个状态格 + 警告/兼容性提示。 */
function renderStatusHeader(view) {
  const env = view.env || {}
  const status = view.status || {}
  const compat = compatState(env, status)
  const gateway = gatewayState(env)
  const patch = patchState(status, view.activeCount)
  const hermesVersion = asText(env.version) || asText(status.hermes_version) || '未知'
  const hfkVersion = asText(status.hfk_version) || asText(env.hfk_version) || asText(view.conn && view.conn.hfkVersion) || '未知'
  const configReadable = env.config_readable === true
  const warnings = asArray(env.warnings).map(item => String(item))
  const configError = asText(env.config_error)
  const versionDetailParts = []
  if (env.version_source) {
    versionDetailParts.push(`来源 ${env.version_source}`)
  }
  if (env.git_dirty === true) {
    versionDetailParts.push('安装目录有未提交修改')
  }

  return jsxs('div', {
    children: [
      jsxs('div', {
        style: STYLES.header,
        children: [
          jsxs('div', {
            style: { minWidth: 0 },
            children: [
              jsxs('div', {
                style: STYLES.inline,
                children: [
                  jsx('h1', { style: STYLES.h1, children: 'HFK · Hermes Freedom Kit' }),
                  jsx(Badge, { variant: 'muted', children: '字段级 · 可逆 · 带备份' })
                ]
              }),
              jsx('p', {
                style: STYLES.subtitle,
                children: '以字段级、可恢复的方式调整 Hermes 本地审批策略（approvals.*）。所有写入由 HFK Core 本地服务执行。'
              })
            ]
          }),
          jsx('div', {
            style: STYLES.actions,
            children: [
              jsx(Button, {
                variant: 'secondary',
                size: 'sm',
                onClick: view.onRefresh,
                loading: view.busy === 'refresh',
                disabled: Boolean(view.busy),
                children: '刷新数据'
              }),
              jsx(Button, {
                variant: 'outline',
                size: 'sm',
                onClick: view.onReconnect,
                disabled: Boolean(view.busy),
                children: '重新连接'
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        style: STYLES.statGrid,
        children: [
          jsx(StatTile, {
            label: 'Hermes 版本',
            value: hermesVersion,
            detail: versionDetailParts.join(' · ') || '来源未知'
          }),
          jsx(StatTile, { label: '兼容性', value: compat.label, detail: compat.reason || '无额外说明', tone: compat.tone }),
          jsx(StatTile, { label: 'Gateway', value: gateway.value, detail: gateway.detail, tone: gateway.tone }),
          jsx(StatTile, { label: 'HFK 版本', value: hfkVersion, detail: asText(view.conn && view.conn.source) || '来自本地服务' }),
          jsx(StatTile, { label: '补丁状态', value: patch.value, detail: patch.detail, tone: patch.tone }),
          jsx(StatTile, {
            label: '配置可读性',
            value: configReadable ? '可读' : '不可读',
            detail: configReadable ? asText(env.config_path) : configError || 'HFK 将拒绝写入',
            tone: configReadable ? CSS.green : CSS.red
          })
        ]
      }),
      compat.supported === false
        ? jsx('div', {
            style: { marginTop: '10px' },
            children: jsx(Notice, {
              kind: 'error',
              title: '当前 Hermes 版本未通过兼容性验证',
              message: compat.reason || 'HFK 只允许查看、备份与导出；禁止应用新补丁。'
            })
          })
        : null,
      compat.supported === true && compat.exact === false
        ? jsx('div', {
            style: { marginTop: '10px' },
            children: jsx(Notice, {
              kind: 'warn',
              title: '同族版本：允许运行，但应用前必须重新预览',
              message: compat.reason || '字段可能仍然存在，但未逐字段验证。'
            })
          })
        : null,
      warnings.length > 0
        ? jsx('div', {
            style: { marginTop: '10px' },
            children: jsx(Notice, {
              kind: 'warn',
              title: '环境提示',
              children: jsx('ul', {
                style: STYLES.bannerList,
                children: warnings.map((warning, index) => jsx('li', { children: warning }, `warning-${index}`))
              })
            })
          })
        : null,
      view.loadErrors && view.loadErrors.length > 0
        ? jsx('div', {
            style: { marginTop: '10px' },
            children: jsx(Notice, {
              kind: 'warn',
              title: '部分数据加载失败（其余区块仍可用）',
              children: jsx('ul', {
                style: STYLES.bannerList,
                children: view.loadErrors.map(item => {
                  const described = describeError(item.error)
                  return jsx(
                    'li',
                    { children: `${item.key}：${described.title} — ${described.message || described.hint}` },
                    `load-error-${item.key}`
                  )
                })
              })
            })
          })
        : null
    ]
  })
}

/** 预设选择区：四张卡片 + 自由预设的风险确认。 */
function renderPresetSection(view) {
  const presetPayload = view.presets
  const catalogue = view.catalogue
  const selectedPreset = view.preset
  const presetOrder = view.presetOrder
  const requiresAck = view.requiresAck
  const specMap = view.specMap || {}

  const cards = presetOrder.map(key => {
    const meta = presetMeta(presetPayload, key)
    let summary = { count: 0, items: [] }
    if (key === 'custom') {
      const enabled = []
      for (const spec of asArray(catalogue && catalogue.fields)) {
        const row = view.selection && view.selection[spec.path]
        if (!row || !row.enabled) {
          continue
        }
        const current = view.currentByPath[spec.path] || { exists: false, value: null }
        enabled.push({
          path: spec.path,
          title: spec.title,
          risk: spec.risk,
          from: fmtValue(current.value, current.exists),
          to: fmtValue(row.value, true)
        })
      }
      summary = { count: enabled.length, items: enabled.slice(0, 3) }
    } else {
      summary = presetChangeSummary(
        view.presetPreviews ? view.presetPreviews[key] : null,
        declaredPresetChanges(presetPayload, catalogue, key),
        view.currentByPath,
        specMap,
        3
      )
    }
    const active = key === selectedPreset
    const isDefault = presetPayload && asText(presetPayload.default) === key
    return jsxs(
      'div',
      {
        role: 'radio',
        'aria-checked': active,
        tabIndex: 0,
        onClick: () => view.onSelectPreset(key),
        onKeyDown: event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            view.onSelectPreset(key)
          }
        },
        style: active ? { ...STYLES.presetCard, ...STYLES.presetCardActive } : STYLES.presetCard,
        children: [
          jsxs('div', {
            style: STYLES.presetTitleRow,
            children: [
              jsx(Codicon, { name: active ? 'circle-filled' : 'circle-large-outline', style: { color: active ? CSS.accent : CSS.textTertiary } }),
              jsx('span', { style: { fontWeight: 600, fontSize: FS.body }, children: meta.label }),
              isDefault ? jsx(Badge, { variant: 'muted', children: '默认' }) : null,
              meta.requiresAcknowledgement ? jsx(Badge, { variant: 'warn', children: '需确认风险' }) : null,
              active && view.alreadyActive
                ? jsx(Badge, { variant: 'muted', children: '已生效 · 无需应用' })
                : null,
              jsx('div', { style: STYLES.spacer }),
              jsx(Badge, { variant: summary.count > 0 ? 'default' : 'muted', children: `改动 ${summary.count} 项` })
            ]
          }),
          jsx('p', { style: STYLES.presetDesc, children: meta.description || '（无描述）' }),
          summary.items.length > 0
            ? jsx('div', {
                style: { display: 'flex', flexDirection: 'column', gap: '2px' },
                children: summary.items.map(item =>
                  jsxs(
                    'div',
                    {
                      style: STYLES.presetChange,
                      children: [
                        jsx('span', { style: STYLES.presetChangePath, title: item.path, children: item.title || item.path }),
                        jsx('span', { style: { whiteSpace: 'nowrap' }, children: `${item.from} → ${item.to}` })
                      ]
                    },
                    `preset-change-${key}-${item.path}`
                  )
                )
              })
            : jsx('div', { style: STYLES.caption, children: '当前配置已与该预设一致，没有需要写入的字段。' }),
          meta.riskNote
            ? jsx('div', { style: key === 'freedom' ? STYLES.callout : STYLES.calloutWarn, children: meta.riskNote })
            : null,
          key === 'freedom' && active && requiresAck
            ? jsxs('label', {
                style: { display: 'flex', alignItems: 'flex-start', gap: '8px', cursor: 'pointer' },
                onClick: event => event.stopPropagation(),
                children: [
                  jsx(Checkbox, {
                    checked: Boolean(view.acknowledged),
                    onCheckedChange: next => view.onAcknowledge(Boolean(next)),
                    'aria-label': '确认自由预设的风险'
                  }),
                  jsx('span', {
                    style: { fontSize: FS.small, color: CSS.textSecondary },
                    children: '我已理解：危险命令不再逐条询问，无人值守平台将完全无人把关。仍要应用「自由」预设。'
                  })
                ]
              })
            : null
        ]
      },
      `preset-card-${key}`
    )
  })

  return jsx(Section, {
    title: 'Freedom Profile 预设',
    hint: '预设只是一组字段取值的别名。选择预设后，仍可在下方逐项覆盖或取消任何一项（包括取消预设本身的改动）。',
    badge: jsx(Badge, { variant: 'muted', children: '默认：平衡' }),
    children: jsxs('div', {
      children: [
        jsx('div', { style: STYLES.grid, role: 'radiogroup', 'aria-label': 'Freedom Profile 预设', children: cards }),
        requiresAck && !view.acknowledged
          ? jsx('div', {
              style: { marginTop: '8px' },
              children: jsx(Notice, {
                kind: 'warn',
                title: '「自由」预设需要显式确认',
                message: '请先在上方「自由」卡片内勾选风险确认，否则「应用」按钮保持禁用。预览不受影响。'
              })
            })
          : null
      ]
    })
  })
}

// =============================================================================
// 3) 逐项配置区（A/B 可交互，C 只读解释）
// -----------------------------------------------------------------------------
// 风险分级与 Python 端 hfk/schema.py 完全一致：A 普通可恢复、B 高风险需逐项开启、
// C 永不写入。这里的顺序也刻意按 A → B → C 排列，让用户先看到安全的那些。
// =============================================================================

/** 一个枚举下拉。 */
function EnumEditor(props) {
  const spec = props.spec
  const values = asArray(spec.values).map(item => String(item))
  return jsxs(Select, {
    value: String(props.value),
    onValueChange: next => props.onChange(String(next)),
    children: [
      jsx(SelectTrigger, { style: { width: '132px' }, children: jsx(SelectValue, {}) }),
      jsx(SelectContent, {
        children: values.map(item =>
          jsx(SelectItem, { value: item, children: item }, `${spec.path}:${item}`)
        )
      })
    ]
  })
}

/** 一个布尔开关。 */
function BoolEditor(props) {
  return jsx(Switch, {
    checked: props.value === true,
    onCheckedChange: next => props.onChange(next === true)
  })
}

/** 一个整数输入（失焦/回车才提交，避免每敲一个数字就重算整页）。 */
function IntEditor(props) {
  const [draft, setDraft] = useState(String(props.value))
  useEffect(() => {
    setDraft(String(props.value))
  }, [props.value])
  const commit = useCallback(() => {
    const parsed = Number(draft)
    if (!Number.isFinite(parsed) || String(parsed) !== draft.trim()) {
      props.onInvalid(`需要一个整数，收到 ${JSON.stringify(draft)}`)
      setDraft(String(props.value))
      return
    }
    props.onChange(Math.trunc(parsed))
  }, [draft, props])
  return jsx(Input, {
    style: { width: '104px' },
    value: draft,
    inputMode: 'numeric',
    onChange: event => setDraft(event.target.value),
    onBlur: commit,
    onKeyDown: event => {
      if (event.key === 'Enter') {
        commit()
      }
    }
  })
}

/** 字符串/列表编辑：列表用逗号分隔，交给纯函数解析。 */
function TextEditor(props) {
  const listMode = props.spec.kind === 'list'
  const initial = listMode ? formatListText(props.value) : String(props.value === null || props.value === undefined ? '' : props.value)
  const [draft, setDraft] = useState(initial)
  useEffect(() => {
    setDraft(initial)
  }, [initial])
  const commit = useCallback(() => {
    props.onChange(listMode ? parseListText(draft) : draft)
  }, [draft, listMode, props])
  return jsx(Textarea, {
    rows: listMode ? 2 : 2,
    style: { width: '260px', resize: 'vertical' },
    value: draft,
    onChange: event => setDraft(event.target.value),
    onBlur: commit,
    placeholder: listMode ? '每行或用逗号分隔一个条目；留空表示空列表' : '留空表示空字符串'
  })
}

/** 单行字段：标题 + 风险 + 当前/目标 + 编辑器 + 解释。 */
function FieldRow(props) {
  const spec = props.spec
  const view = props.view
  const row = view.selection[spec.path] || { enabled: false, value: spec.default }
  const current = view.currentByPath[spec.path] || { exists: false, value: null }
  const risk = asText(spec.risk) || 'C'
  const interactive = risk !== 'C'
  // 只有勾选的字段才校验：未勾选的项不参与本次补丁，报错只会干扰视线。
  const errors = row.enabled
    ? validateFieldValue(spec, coerceValue(asText(spec.kind), row.value))
    : []

  const editor = (() => {
    if (!row.enabled) {
      return null
    }
    const onChange = next => view.onChangeField(spec.path, next)
    const onInvalid = message => view.onFieldError(spec.path, message)
    if (spec.kind === 'enum') {
      return jsx(EnumEditor, { spec, value: row.value, onChange })
    }
    if (spec.kind === 'bool') {
      return jsx(BoolEditor, { spec, value: row.value, onChange })
    }
    if (spec.kind === 'int') {
      return jsx(IntEditor, { spec, value: row.value, onChange, onInvalid })
    }
    return jsx(TextEditor, { spec, value: row.value, onChange })
  })()

  return jsxs('div', {
    style: STYLES.fieldRow,
    children: [
      jsxs('div', {
        style: STYLES.fieldMain,
        children: [
          jsxs('div', {
            style: STYLES.fieldTitleRow,
            children: [
              interactive
                ? jsx(Checkbox, {
                    checked: row.enabled === true,
                    onCheckedChange: next => view.onToggleField(spec.path, next === true),
                    'aria-label': `启用 ${spec.title}`
                  })
                : null,
              jsx('span', { style: STYLES.label, children: asText(spec.title) || spec.path }),
              jsx(RiskBadge, { risk }),
              jsx('code', { style: STYLES.caption, children: spec.path })
            ]
          }),
          jsx('p', {
            style: STYLES.fieldDiff,
            children: [
              '当前 ',
              jsx('code', { style: STYLES.mono, children: fmtValue(current.value, current.exists) }),
              ' → 目标 ',
              jsx('code', {
                style: STYLES.mono,
                children: row.enabled ? fmtValue(row.value, true) : '（不修改）'
              })
            ]
          }),
          spec.effect ? jsx('p', { style: STYLES.fieldEffect, children: spec.effect }) : null,
          errors.length
            ? jsx('p', { style: STYLES.errorText, children: errors.join('；') })
            : null,
          spec.evidence
            ? jsxs('details', {
                style: STYLES.details,
                children: [
                  jsx('summary', { style: STYLES.summary, children: '源码证据 / 影响范围' }),
                  jsx('div', { style: { marginTop: '4px' }, children: spec.evidence }),
                  asArray(spec.surfaces).length
                    ? jsx('div', { children: `影响路径：${asArray(spec.surfaces).join(' / ')}` })
                    : null,
                  spec.notes ? jsx('div', { children: spec.notes }) : null
                ]
              })
            : null
        ]
      }),
      jsx('div', { style: STYLES.fieldEditor, children: editor })
    ]
  }, spec.path)
}

function renderFieldSection(view) {
  const catalogue = view.catalogue
  const fields = asArray(catalogue && catalogue.fields)
  const interactive = fields.filter(spec => asText(spec.risk) !== 'C')
  const blocked = asArray(catalogue && catalogue.blocked)
  const floors = asArray(catalogue && catalogue.code_level_floors)

  return jsxs(Section, {
    title: '逐项配置',
    hint:
      `共 ${interactive.length} 项可调。A 类可放心勾选；B 类会削弱某类检测，默认不选，必须你逐项打开；C 类 HFK 永不写入。` +
      '勾选与改值都会进入下一次「预览」，不会立即写入。',
    children: [
      jsx('div', {
        style: STYLES.card,
        children: interactive.length
          ? interactive.map(spec => jsx(FieldRow, { spec, view }, spec.path))
          : jsx('div', { style: STYLES.empty, children: '字段清单为空——请检查 HFK Core 服务版本。' })
      }),
      blocked.length
        ? jsxs(Section, {
            title: 'HFK 明确不修改（C 类）',
            hint: '这些不是「忘了做」，而是刻意排除。原因逐条给出。',
            children: blocked.map(item =>
              jsxs('div', {
                style: STYLES.blockedRow,
                children: [
                  jsx(Badge, { variant: 'muted', children: 'C' }),
                  jsx('code', { style: STYLES.mono, children: asText(item.path) }),
                  jsx('span', { style: STYLES.caption, children: asText(item.reason) })
                ]
              }, `blocked:${item.path}`)
            )
          })
        : null,
      floors.length
        ? jsxs(Section, {
            title: '代码级安全底线（不可配置，因此无法被本工具削弱）',
            hint: '即使 approvals.mode=off（--yolo），这些保护仍然生效。',
            children: floors.map(item =>
              jsxs('div', {
                style: STYLES.floorRow,
                children: [
                  jsx(Codicon, { name: 'shield' }),
                  jsx('span', { style: STYLES.label, children: asText(item.name) }),
                  jsx('span', { style: STYLES.caption, children: asText(item.detail) })
                ]
              }, `floor:${item.name}`)
            )
          })
        : null
    ]
  })
}

// =============================================================================
// 4) 补丁管理 + 历史
// =============================================================================

/** 变更差异表（预览 / 应用结果共用）。 */
function renderChangeTable(changes) {
  const rows = asArray(changes)
  if (!rows.length) {
    return jsx('div', { style: STYLES.empty, children: '没有变更。' })
  }
  return jsxs('table', {
    style: STYLES.table,
    children: [
      jsx('thead', {
        children: jsx('tr', {
          children: [
            jsx('th', { style: STYLES.th, children: '字段' }),
            jsx('th', { style: STYLES.th, children: '当前' }),
            jsx('th', { style: STYLES.th, children: '目标' }),
            jsx('th', { style: STYLES.th, children: '状态' })
          ]
        })
      }),
      jsx('tbody', {
        children: rows.map((change, index) => {
          const state = change.error
            ? { label: '错误', tone: CSS.red }
            : change.changed === true
              ? { label: '将修改', tone: CSS.orange }
              : { label: '无变化', tone: CSS.textTertiary }
          return jsxs('tr', {
            children: [
              jsx('td', { style: STYLES.td, children: jsx('code', { style: STYLES.caption, children: asText(change.path) }) }),
              jsx('td', {
                style: STYLES.td,
                children: jsx('code', { style: STYLES.mono, children: fmtValue(change.before, change.before_exists) })
              }),
              jsx('td', {
                style: STYLES.td,
                children: jsxs('code', {
                  style: { ...STYLES.mono, color: state.tone },
                  children: [fmtValue(change.after, true), change.note ? ` ${change.note}` : '']
                })
              }),
              jsx('td', {
                style: { ...STYLES.td, color: state.tone },
                children: change.error ? String(change.error) : state.label
              })
            ]
          }, `${asText(change.path)}:${index}`)
        })
      })
    ]
  })
}

function renderPatchSection(view) {
  const preview = view.preview
  const summary = preview ? previewSummary(preview) : null
  const selectionErrors = collectSelectionErrors(view.catalogue, view.selection)

  return jsxs(Section, {
    title: '补丁管理',
    hint: '顺序固定为：预览 → 应用（自动备份 + 事务记录）→ 随时按字段恢复。恢复时若你改过某个字段，HFK 会保留你的值并报冲突。',
    children: [
      jsxs('div', {
        style: STYLES.actions,
        children: [
          jsx(Button, {
            variant: 'secondary',
            onClick: view.onPreview,
            loading: view.busy === 'preview',
            disabled: Boolean(view.busy) || selectionErrors.length > 0,
            children: '预览差异'
          }),
          jsx(Button, {
            variant: 'primary',
            onClick: view.onApply,
            loading: view.busy === 'apply',
            disabled:
              Boolean(view.busy) ||
              selectionErrors.length > 0 ||
              !preview ||
              !summary ||
              !summary.hasEffect ||
              (view.requiresAck && !view.ack),
            children: '应用补丁'
          }),
          jsx(Button, {
            variant: 'outline',
            onClick: view.onRestore,
            loading: view.busy === 'restore',
            disabled: Boolean(view.busy) || (Number(view.status && view.status.pending_transactions) || 0) === 0,
            children: '恢复全部'
          }),
          jsx(Button, {
            variant: 'ghost',
            onClick: view.onUninstall,
            loading: view.busy === 'uninstall',
            disabled: Boolean(view.busy),
            children: '卸载 HFK'
          })
        ]
      }),
      view.requiresAck
        ? jsxs('div', {
            style: { ...STYLES.callout, ...STYLES.calloutWarn },
            children: [
              jsx(Codicon, { name: 'warning' }),
              jsxs('div', {
                children: [
                  jsx('strong', { children: '「自由」预设需要显式确认' }),
                  jsx('p', {
                    style: STYLES.caption,
                    children:
                      '它会把审批主模式切到 off（等同 --yolo），并放开 cron / 单次查询 / 无人值守三类场景。HARDLINE 硬阻断与 approvals.deny 仍然生效。'
                  }),
                  jsxs('label', {
                    style: { ...STYLES.inline, cursor: 'pointer' },
                    children: [
                      jsx(Checkbox, {
                        checked: view.ack === true,
                        onCheckedChange: next => view.onAck(next === true)
                      }),
                      jsx('span', { style: STYLES.caption, children: '我理解风险，允许应用「自由」预设' })
                    ]
                  })
                ]
              })
            ]
          })
        : null,
      selectionErrors.length
        ? jsx(Notice, {
            kind: 'error',
            title: '选择集有错误，已禁用预览与应用',
            message: selectionErrors.join('；')
          })
        : null,
      view.error
        ? jsx(Notice, {
            kind: 'error',
            title: '操作失败',
            message: asText(view.error.message) || '未知错误',
            detail: asText(view.error.detail)
          })
        : null,
      view.notice
        ? jsx(Notice, {
            kind: view.notice.kind || 'info',
            title: view.notice.title,
            message: view.notice.message,
            detail: view.notice.detail,
            onDismiss: view.onDismissNotice
          })
        : null,
      preview
        ? jsxs('div', {
            style: { marginTop: '12px' },
            children: [
              jsxs('div', {
                style: STYLES.inline,
                children: [
                  jsx('strong', { children: '预览结果' }),
                  jsx(Badge, { variant: summary.changed ? 'warn' : 'muted', children: `将修改 ${summary.changed} 项` }),
                  jsx(Badge, { variant: 'muted', children: `无变化 ${summary.unchanged} 项` }),
                  summary.failed ? jsx(Badge, { variant: 'danger', children: `错误 ${summary.failed} 项` }) : null,
                  jsx('code', { style: STYLES.caption, children: `config sha256 ${asText(preview.expected_sha256).slice(0, 16)}…` })
                ]
              }),
              asArray(preview.warnings).map((warning, index) =>
                jsx(Notice, { kind: 'warn', message: String(warning) }, `warn:${index}`)
              ),
              renderChangeTable(preview.changes)
            ]
          })
        : null,
      view.restoreReport
        ? jsxs('div', {
            style: { marginTop: '12px' },
            children: [
              jsxs('div', {
                style: STYLES.inline,
                children: [
                  jsx('strong', { children: '恢复结果' }),
                  jsx(Badge, {
                    variant: view.restoreReport.conflicts && view.restoreReport.conflicts.length ? 'warn' : 'success',
                    children: asText(view.restoreReport.status)
                  }),
                  jsx(Badge, { variant: 'muted', children: `已还原 ${asArray(view.restoreReport.restored).length} 项` })
                ]
              }),
              asArray(view.restoreReport.conflicts).length
                ? jsxs('div', {
                    style: { marginTop: '6px' },
                    children: [
                      jsx('div', { style: STYLES.caption, children: '以下字段检测到你的后续修改，已保留你的值：' }),
                      jsx('ul', {
                        style: STYLES.list,
                        children: asArray(view.restoreReport.conflicts).map((conflict, index) =>
                          jsx(
                            'li',
                            {
                              children: `${asText(conflict.path)}：当前 ${fmtValue(conflict.current, conflict.current_exists)}，HFK 曾写入 ${fmtValue(conflict.hfk_wrote, true)}`
                            },
                            `conflict:${index}`
                          )
                        )
                      })
                    ]
                  })
                : null,
              asArray(view.restoreReport.warnings).map((warning, index) =>
                jsx(Notice, { kind: 'warn', message: String(warning) }, `rwarn:${index}`)
              )
            ]
          })
        : null
    ]
  })
}

/** 历史页签：备份 / 事务。 */
function renderHistorySection(view) {
  const tabButtons = [
    ['backups', '备份历史'],
    ['transactions', '事务历史']
  ]
  return jsxs(Section, {
    title: '历史与审计',
    hint: '备份与事务都由 HFK Core 保存在 <HERMES_HOME>/hfk/ 下；卸载时也会保留，供你随时回退或审计。',
    children: [
      jsx('div', {
        style: STYLES.actions,
        children: tabButtons.map(entry =>
          jsx(
            Button,
            {
              variant: view.historyTab === entry[0] ? 'secondary' : 'ghost',
              size: 'sm',
              onClick: () => view.onSelectHistoryTab(entry[0]),
              children: entry[1]
            },
            `tab:${entry[0]}`
          )
        )
      }),
      jsx('div', {
        style: STYLES.tabPanel,
        children:
          view.historyTab === 'backups' ? renderBackups(view) : renderTransactions(view)
      })
    ]
  })
}

function renderBackups(view) {
  const payload = view.backups || {}
  const files = asArray(payload.files)
  return jsxs('div', {
    children: [
      jsx('div', { style: STYLES.caption, children: `目录：${asText(payload.directory) || '未知'}` }),
      files.length
        ? jsx('ul', {
            style: STYLES.list,
            children: files.map((file, index) =>
              jsx('li', { children: `${asText(file.name)}  ·  ${fmtBytes(file.size)}` }, `backup:${index}`)
            )
          })
        : jsx('div', { style: STYLES.empty, children: '还没有备份。第一次成功 Apply 之后会自动生成。' })
    ]
  })
}

function renderTransactions(view) {
  const payload = view.transactions || {}
  const items = asArray(payload.transactions)
  if (!items.length) {
    return jsx('div', { style: STYLES.empty, children: '还没有事务记录。' })
  }
  return jsx('div', {
    children: items
      .slice()
      .reverse()
      .map((tx, index) => {
        const meta = transactionMeta(tx.status)
        return jsxs('div', {
          style: { ...STYLES.card, marginTop: index === 0 ? 0 : '8px' },
          children: [
            jsxs('div', {
              style: STYLES.inline,
              children: [
                jsx(Badge, { variant: meta.tone, children: meta.label }),
                jsx('code', { style: STYLES.monoStrong, children: asText(tx.transaction_id) }),
                jsx('span', { style: STYLES.caption, children: fmtTimestamp(tx.timestamp) }),
                jsx('span', { style: STYLES.caption, children: `预设 ${asText(tx.preset)}` }),
                jsx('span', { style: STYLES.caption, children: `Hermes ${asText(tx.hermes_version)}` })
              ]
            }),
            jsx('ul', {
              style: STYLES.list,
              children: asArray(tx.changes).map((change, changeIndex) =>
                jsx(
                  'li',
                  {
                    children: `${asText(change.path)}：${fmtValue(change.before, change.before_exists)} → ${fmtValue(change.after, true)}（${asText(change.status)}）`
                  },
                  `chg:${changeIndex}`
                )
              )
            }),
            tx.backup_path
              ? jsx('div', { style: STYLES.caption, children: `备份：${asText(tx.backup_path)}` })
              : null
          ]
        }, asText(tx.transaction_id) || `tx:${index}`)
      })
  })
}

// =============================================================================
// 5) 主页面
// =============================================================================

/** 从 store + 预设 + 选择集组装出各 render* 需要的 view。 */
function makeView(state, handlers) {
  const { store, presetPreviews, preset, selection, ack, busy, error, notice, preview, restoreReport, historyTab, conn, loadErrors, activePreset } = state
  const catalogue = store.catalogue
  const specMap = specsByPath(catalogue)
  const currentByPath = configFieldMap(store.config)
  const presetMetaForSelected = presetMeta(store.presets, preset)
  const activeFields = store.status && isPlainObject(store.status.active_fields) ? store.status.active_fields : {}

  // 预设已生效、且选择集没有被改写 → 应用不会写入任何东西，
  // 此时不该再要求确认风险（否则就是为一个 no-op 弹风险确认）。
  const alreadyActive =
    Boolean(activePreset) &&
    activePreset === preset &&
    selectionMatchesDeclared(
      selection,
      declaredPresetChanges(store.presets, catalogue, preset)
    )

  return {
    env: store.env,
    status: store.status,
    catalogue,
    presets: store.presets,
    transactions: store.transactions,
    backups: store.backups,
    presetPreviews,
    preset,
    presetOrder: PRESET_KEYS,
    selection,
    specMap,
    currentByPath,
    requiresAck: presetMetaForSelected.requiresAcknowledgement === true && !alreadyActive,
    alreadyActive,
    activePreset,
    acknowledged: ack,
    loadErrors: asArray(loadErrors),
    // 预置渲染函数用的别名（保持与既有 render* 的契约一致）
    onAcknowledge: handlers.onAck,
    onReconnect: handlers.onRefresh,
    ack,
    busy,
    error,
    notice,
    preview,
    restoreReport,
    historyTab,
    activeCount: Object.keys(activeFields).length,
    conn,
    ...handlers
  }
}

function HfkPage(props) {
  const storage = props && props.storage ? props.storage : null
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const [conn, setConn] = useState(null)
  const [client, setClient] = useState(null)
  const [store, setStore] = useState({
    env: null,
    status: null,
    catalogue: null,
    presets: null,
    config: null,
    transactions: null,
    backups: null
  })
  const [presetPreviews, setPresetPreviews] = useState({})
  // 选中态优先用上次记住的，避免切走再回来跳回默认档
  const [preset, setPreset] = useState(() => loadStoredPreset(storage) || 'balanced')
  // 「磁盘上真正生效的预设」——从 config.yaml 反推，与选中态是两件事
  const [activePreset, setActivePreset] = useState('')
  const [selection, setSelection] = useState({})
  const [ack, setAck] = useState(false)
  const [busy, setBusy] = useState('connect')
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [preview, setPreview] = useState(null)
  const [restoreReport, setRestoreReport] = useState(null)
  const [historyTab, setHistoryTab] = useState('backups')
  const [overrides, setOverrides] = useState({ baseUrl: '', token: '' })
  const [loadErrors, setLoadErrors] = useState([])
  const [phase, setPhase] = useState('connecting')

  // ---- 连接 + 首次加载 -----------------------------------------------------
  const connect = useCallback(async () => {
    setBusy('connect')
    setError(null)
    setPhase('connecting')
    try {
      const connection = await connectToService(overrides)
      if (connection.phase !== 'ready' || !connection.conn) {
        setError(connection.error || makeApiError('unreachable', '无法连接 HFK Core 服务'))
        setPhase('offline')
        setBusy(null)
        return
      }
      const asClient = connection.conn.client
      setClient(asClient)
      setConn(connection.conn)
      const loaded = await loadStore(asClient)
      if (!mountedRef.current) {
        return
      }
      setStore(loaded.data)
      setLoadErrors(loaded.errors)
      // 从磁盘反推真正生效的预设；用户从未手动选过时，直接对齐到它
      const detected = detectActivePreset(
        loaded.data.presets,
        loaded.data.catalogue,
        configFieldMap(loaded.data.config)
      )
      setActivePreset(detected || '')
      if (detected && !loadStoredPreset(storage)) {
        setPreset(detected)
      }
      if (loaded.unauthorized) {
        setError(makeApiError('unauthorized', '令牌无效或已过期（服务重启后令牌会变）'))
      } else if (loaded.errors.length) {
        setError(loaded.errors[0].error)
      }
      const previews = await loadPresetPreviews(asClient)
      if (mountedRef.current) {
        setPresetPreviews(previews)
        setPhase('ready')
      }
    } catch (caught) {
      if (mountedRef.current) {
        setError(describeError(caught))
        setPhase('offline')
      }
    } finally {
      if (mountedRef.current) {
        setBusy(null)
      }
    }
  }, [overrides])

  useEffect(() => {
    setOverrides(loadStoredOverrides(storage))
  }, [storage])

  useEffect(() => {
    connect()
  }, [connect])

  // ---- 选择集随预设/目录/当前值重建 ---------------------------------------
  const declared = useMemo(
    () => declaredPresetChanges(store.presets, store.catalogue, preset),
    [store.presets, store.catalogue, preset]
  )
  const currentByPath = useMemo(() => configFieldMap(store.config), [store.config])

  useEffect(() => {
    // 目录还没到时不要建空选择集，否则会把用户已经勾好的项清掉
    if (!store.catalogue) {
      return
    }
    setSelection(prev => {
      const next = buildSelection(preset, declared, store.catalogue, currentByPath)
      // 自定义模式下保留用户已手动开启的项（buildSelection 只认预设声明）
      if (preset === 'custom' && prev && Object.keys(prev).length) {
        for (const path of Object.keys(prev)) {
          if (prev[path] && prev[path].enabled) {
            next[path] = { ...next[path], ...prev[path] }
          }
        }
      }
      return next
    })
    setPreview(null)
  }, [preset, declared, store.catalogue, currentByPath])

  // 只在「用户切换预设」时清掉风险确认。
  // 数据刷新（连接/预览/应用后）绝不能碰它——否则勾选框会自己弹回未勾选，
  // 表现为「老是自动就没」。
  useEffect(() => {
    setAck(false)
  }, [preset])

  // ---- 交互 ---------------------------------------------------------------
  const onChangeField = useCallback((path, value) => {
    setSelection(prev => ({ ...prev, [path]: { ...(prev[path] || {}), enabled: true, value } }))
    setPreview(null)
  }, [])

  const onToggleField = useCallback((path, enabled) => {
    setSelection(prev => {
      const spec = (store.catalogue && asArray(store.catalogue.fields).find(item => item.path === path)) || null
      const base = prev[path] || {}
      const fallback = spec ? coerceValue(asText(spec.kind), spec.default) : base.value
      return { ...prev, [path]: { ...base, enabled, value: base.value === undefined ? fallback : base.value } }
    })
    setPreview(null)
  }, [store.catalogue])

  const onFieldError = useCallback(message => {
    setError(makeApiError('invalid_input', message))
  }, [])

  const refresh = useCallback(async () => {
    if (!client) {
      return connect()
    }
    setBusy('refresh')
    setError(null)
    try {
      const loaded = await loadStore(client)
      if (!mountedRef.current) {
        return
      }
      setStore(loaded.data)
      setLoadErrors(loaded.errors)
      const detected = detectActivePreset(
        loaded.data.presets,
        loaded.data.catalogue,
        configFieldMap(loaded.data.config)
      )
      setActivePreset(detected || '')
      if (loaded.errors.length) {
        setError(loaded.errors[0].error)
      }
      const previews = await loadPresetPreviews(client)
      if (mountedRef.current) {
        setPresetPreviews(previews)
      }
    } catch (caught) {
      if (mountedRef.current) {
        setError(describeError(caught))
      }
    } finally {
      if (mountedRef.current) {
        setBusy(null)
      }
    }
  }, [client, connect])

  const buildBody = useCallback(() => {
    const body = { preset, overrides: buildOverrides(store.catalogue, selection) }
    if (preset === 'custom') {
      // 自定义模式下只发送用户真正勾选的字段
      body.overrides = buildOverrides(store.catalogue, selection)
      body.preset = 'custom'
    }
    return body
  }, [preset, store.catalogue, selection])

  const doPreview = useCallback(async () => {
    if (!client) {
      return
    }
    setBusy('preview')
    setError(null)
    setRestoreReport(null)
    try {
      const payload = await client.preview(buildBody())
      if (mountedRef.current) {
        setPreview(payload)
        setNotice(null)
      }
    } catch (caught) {
      if (mountedRef.current) {
        setError(describeError(caught))
      }
    } finally {
      if (mountedRef.current) {
        setBusy(null)
      }
    }
  }, [client, buildBody])

  const doApply = useCallback(async () => {
    if (!client || !preview) {
      return
    }
    const summary = previewSummary(preview)
    if (!summary.hasEffect) {
      return
    }
    setBusy('apply')
    setError(null)
    try {
      const body = { ...buildBody(), acknowledge_risk: ack === true }
      const result = await client.apply(body)
      if (mountedRef.current) {
        setNotice({
          kind: 'success',
          title: '补丁已应用',
          message: `事务 ${asText(result.transaction_id)}：修改 ${asArray(result.changes).length} 项`,
          detail: `备份 ${asText(result.backup_path)}`
        })
        setPreview(null)
      }
      await refresh()
    } catch (caught) {
      if (mountedRef.current) {
        setError(describeError(caught))
      }
    } finally {
      if (mountedRef.current) {
        setBusy(null)
      }
    }
  }, [client, preview, buildBody, ack, refresh])

  const doRestore = useCallback(async () => {
    if (!client) {
      return
    }
    setBusy('restore')
    setError(null)
    try {
      const report = await client.restore({})
      if (mountedRef.current) {
        setRestoreReport(report)
        setPreview(null)
      }
      await refresh()
    } catch (caught) {
      if (mountedRef.current) {
        setError(describeError(caught))
      }
    } finally {
      if (mountedRef.current) {
        setBusy(null)
      }
    }
  }, [client, refresh])

  const doUninstall = useCallback(async () => {
    if (!client) {
      return
    }
    setBusy('uninstall')
    setError(null)
    try {
      const report = await client.uninstall({ remove_plugin: false })
      if (mountedRef.current) {
        setRestoreReport(report.restore || null)
        setNotice({
          kind: 'success',
          title: '已安全恢复并卸载',
          message: 'Hermes 本体与你的配置未被删除；备份与事务历史已保留。',
          detail: `保留备份 ${report.kept_backups || 0} 个`
        })
      }
      await refresh()
    } catch (caught) {
      if (mountedRef.current) {
        setError(describeError(caught))
      }
    } finally {
      if (mountedRef.current) {
        setBusy(null)
      }
    }
  }, [client, refresh])

  // ---- 渲染 ---------------------------------------------------------------
  if (phase === 'connecting' && !store.env) {
    return jsxs('div', {
      style: STYLES.centerLoading,
      children: [
        jsx(Codicon, { name: 'loading', className: 'codicon-modifier-spin' }),
        jsx('div', { children: '正在连接 HFK Core 服务……' })
      ]
    })
  }

  if (phase === 'offline') {
    return jsxs('div', {
      style: STYLES.page,
      children: [
        jsx(Notice, {
          kind: 'error',
          title: '连不上 HFK Core 服务',
          message: asText(error && error.message) || '没有找到可用的本地服务',
          detail: asText(error && error.detail)
        }),
        jsxs(Section, {
          title: '怎么修',
          hint: '插件只负责界面；真正写配置的是本机 HFK Core 服务，必须先把它启动起来。',
          children: [
            jsx('div', { style: STYLES.caption, children: '1) 在 HFK 项目目录里启动服务（只监听 127.0.0.1）：' }),
            jsx(CodeLine, { children: `cd E:\\harness\\soudushi\\Hermes-Freedom-Kit\n${SERVICE_COMMAND_PYTHON}` }),
            jsx('div', { style: STYLES.caption, children: '2) 服务会写一个令牌发现文件，插件会自己读：' }),
            jsx(CodeLine, { children: `[HERMES_HOME]\\${SERVICE_FILE_RELATIVE.replace('/', '\\')}` }),
            jsx('div', { style: STYLES.caption, children: '3) 如果文件读不到，可以在下方手动填地址与令牌。' }),
            jsx('div', {
              style: { marginTop: '10px' },
              children: jsx(Button, { variant: 'secondary', onClick: connect, children: '重试连接' })
            }),
            jsx(ConnectionForm, {
              overrides,
              onSave: next => {
                persistBaseUrl(storage, next.baseUrl)
                setOverrides(next)
              }
            })
          ]
        })
      ]
    })
  }

  const view = makeView(
    { store, presetPreviews, preset, selection, ack, busy, error, notice, preview, restoreReport, historyTab, conn, loadErrors, activePreset },
    {
      onRefresh: refresh,
      onSelectPreset: key => {
        setPreset(key)
        persistPreset(storage, key)
      },
      onToggleField,
      onChangeField,
      onFieldError,
      onPreview: doPreview,
      onApply: doApply,
      onRestore: doRestore,
      onUninstall: doUninstall,
      onAck: setAck,
      onDismissNotice: () => setNotice(null),
      onSelectHistoryTab: setHistoryTab
    }
  )

  return jsxs('div', {
    style: STYLES.page,
    children: [
      renderStatusHeader(view),
      jsx('div', {
        style: { ...STYLES.caption, marginTop: '14px' },
        children: activePreset
          ? `config.yaml 当前生效：${presetMeta(store.presets, activePreset).label}（${activePreset}）—— 这是从磁盘实测的，与你下面选哪张卡无关。`
          : 'config.yaml 当前生效：与任何预设都不完全一致（自定义组合）。'
      }),
      renderPresetSection(view),
      renderFieldSection(view),
      renderPatchSection(view),
      renderHistorySection(view),
      jsx('p', {
        style: { ...STYLES.caption, marginTop: '20px' },
        children: [
          `服务 ${asText(conn && conn.baseUrl)}`,
          ` · 连接来源 ${asText(conn && conn.source) || '未知'}`,
          ` · 数据目录 ${asText(store.env && store.env.hfk_dir) || '未知'}`,
          ` · UI build ${UI_BUILD}`
        ].join('')
      })
    ]
  })
}

// =============================================================================
// 6) 设置页（Settings ▸ Plugins ▸ HFK）
// =============================================================================

/** 连接覆盖表单：地址与令牌。令牌只写不读（不回显）。 */
function ConnectionForm(props) {
  const [baseUrl, setBaseUrl] = useState(props.overrides ? props.overrides.baseUrl || '' : '')
  const [token, setToken] = useState(props.overrides ? props.overrides.token || '' : '')
  return jsxs('div', {
    style: STYLES.connectionForm,
    children: [
      jsx('label', {
        style: STYLES.connectionLabel,
        children: '服务地址（留空则自动从 service.json 发现）'
      }),
      jsx(Input, {
        value: baseUrl,
        placeholder: 'http://127.0.0.1:端口',
        onChange: event => setBaseUrl(event.target.value)
      }),
      jsx('label', { style: STYLES.connectionLabel, children: '访问令牌（X-HFK-Token）' }),
      jsx(Input, {
        value: token,
        type: 'password',
        placeholder: '从 service.json 读取，或粘贴 --print-token 的输出',
        onChange: event => setToken(event.target.value)
      }),
      jsxs('div', {
        style: STYLES.actions,
        children: [
          jsx(Button, {
            variant: 'secondary',
            size: 'sm',
            onClick: () => props.onSave({ baseUrl: baseUrl.trim(), token: token.trim() }),
            children: '保存并重连'
          }),
          jsx(Button, {
            variant: 'ghost',
            size: 'sm',
            onClick: () => {
              setBaseUrl('')
              setToken('')
              props.onSave({ baseUrl: '', token: '' })
            },
            children: '清除覆盖（回到自动发现）'
          })
        ]
      }),
      jsx('p', {
        style: STYLES.caption,
        children: '安全提示：令牌每次服务重启都会变；HFK 不会把它写进日志，也不会在响应里回显。'
      })
    ]
  })
}

function HfkSettingsPage(props) {
  const storage = props && props.storage ? props.storage : null
  const [overrides, setOverrides] = useState({ baseUrl: '', token: '' })
  useEffect(() => {
    setOverrides(loadStoredOverrides(storage))
  }, [storage])
  return jsxs('div', {
    style: STYLES.page,
    children: [
      jsxs('div', {
        style: STYLES.header,
        children: [
          jsx('h2', { style: STYLES.h1, children: 'HFK 连接设置' }),
          jsx('p', {
            style: STYLES.subtitle,
            children: 'Deskopo 插件通过本机回环地址调用 HFK Core 服务。通常无需填写，插件会自动读取 service.json。'
          })
        ]
      }),
      jsx(Section, {
        title: '连接覆盖',
        hint: '只有在自动发现失败（例如自定义端口、或读不到 service.json）时才需要手动填写。',
        children: jsx(ConnectionForm, {
          overrides,
          onSave: next => {
            persistBaseUrl(storage, next.baseUrl)
            setOverrides(next)
          }
        })
      }),
      jsx(Section, {
        title: '启动命令',
        children: [
          jsx('div', { style: STYLES.caption, children: '在 HFK 项目目录中运行：' }),
          jsx(CodeLine, { children: `${SERVICE_COMMAND_PYTHON}\n# 或已安装启动器：\n${SERVICE_COMMAND_CLI}` })
        ]
      })
    ]
  })
}

// =============================================================================
// 7) 注册与 manifest
// =============================================================================

/**
 * Hermes Desktop 插件入口。
 * - 目录：<HERMES_HOME>/desktop-plugins/hfk/plugin.js
 * - 无 manifest 文件；这个默认导出就是清单。
 * - 所有注册都返回 disposer，由 register 统一收集并在卸载时调用。
 */
const hfkPlugin = {
  id: 'hfk',
  name: 'HFK · Hermes Freedom Kit',
  description: '字段级、可逆、带备份与事务记录的 Hermes 本地审批策略补丁工具（UI）。',
  defaultEnabled: false,

  register(ctx) {
    const disposers = []
    const push = disposer => {
      if (typeof disposer === 'function') {
        disposers.push(disposer)
      }
    }

    // 主页面
    push(
      ctx.register({
        id: 'hfk-page',
        area: ROUTES_AREA,
        data: { path: '/hfk' },
        render: () => jsx(HfkPage, { storage: ctx.storage })
      })
    )

    // 侧栏入口
    push(
      ctx.register({
        id: 'hfk-sidebar',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/hfk', label: 'HFK', codicon: 'shield' }
      })
    )

    // 设置页（可用时）
    if (typeof ctx.registerSettingsPage === 'function') {
      push(
        ctx.registerSettingsPage({
          id: 'hfk-settings',
          title: 'HFK',
          icon: 'shield',
          render: () => jsx(HfkSettingsPage, { storage: ctx.storage })
        })
      )
    }

    return () => {
      for (const dispose of disposers) {
        try {
          dispose()
        } catch (error) {
          /* 单项清理失败不应阻断其它清理 */
        }
      }
    }
  }
}

export default hfkPlugin
