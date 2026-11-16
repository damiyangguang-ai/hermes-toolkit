# HFK Desktop 插件

这是 HFK 的 **Hermes Desktop 界面**。它只负责展示与触发；真正的配置写入由
**HFK Core 本地服务**（Python）完成。二者通过 `127.0.0.1` + 随机令牌通信。

## 安装

1. 启动一次 HFK Core 服务（必须先跑起来，插件才有后端可连）：

   ```powershell
   cd <WORKSPACE>\Hermes-Freedom-Kit
   $py = "<HERMES_VENV_PYTHON>"
   & $py -m hfk.cli service
   ```

   服务会打印地址，并写入令牌发现文件：

   ```
   <HERMES_HOME>\hfk\service.json     # { pid, port, base_url, token, ... }，权限 0600
   ```

2. 把插件复制到 Hermes 的插件目录（入口文件名必须是 `plugin.js`）：

   ```powershell
   $pluginDir = "$env:LOCALAPPDATA\hermes\desktop-plugins\hfk"
   New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
   Copy-Item .\plugin.js -Destination $pluginDir -Force
   ```

   或者直接运行仓库根目录的 `install.ps1`（会顺带完成这一步与自检）。

3. 在 Hermes Desktop 里启用插件：`设置 ▸ 插件（Plugins）`。插件默认是**关闭**的
   （`defaultEnabled: false`），需要你手动打开。

4. 打开左侧栏的 **HFK** 页面，或 `设置 ▸ 插件 ▸ HFK` 查看连接设置。

## 插件做了什么 / 没做什么

| 做 | 不做 |
|---|---|
| 读取 `/api/*`、展示环境、字段清单、预设差异 | 不直接读写 `config.yaml` |
| 触发 preview / apply / restore / uninstall | 不执行任意命令、不 `eval`、不抓宿主 DOM |
| 保存「服务地址」覆盖到插件存储 | **不保存令牌**（只在内存里，页面重载后需重新粘贴） |
| 展示备份与事务历史 | 不读取任何 API Key / 秘密 |

## 令牌与连接

- 正常情况下插件自动读 `<HERMES_HOME>\hfk\service.json`，无需手工配置。
- 每次重启服务令牌都会变；旧令牌立即失效（表现为 `401 unauthorized`）。
- 自动发现失败时，可在 `设置 ▸ 插件 ▸ HFK` 手动填写地址与令牌。
  也可以启动服务时加 `--print-token` 拿到令牌手工粘贴。
- 令牌**不会**被写进日志，也不会在 HTTP 响应里回显，更不会出现在界面上。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面提示「连不上 HFK Core 服务」 | 服务没启动 | 运行 `python -m hfk.cli service`，再点「重试连接」 |
| `401 unauthorized` | 令牌缺失或已过期 | 重新从 `service.json` 读取，或重启服务后重新连接 |
| 只有地址、没有令牌且读不到 `service.json` | 发现文件缺失 | 手动填地址 + 用 `--print-token` 输出的令牌 |
| 侧栏没有 HFK 入口 | 插件未启用 | `设置 ▸ 插件` 里打开 HFK |
| 预览/应用按钮灰掉 | 选择集有非法值，或预览显示「无变化」 | 看按钮上方的红色错误提示，或换一个会真正改变行为的项 |

## 开发说明

- 单文件 ESM，**没有构建步骤**，也没有 `package.json`。保存即热重载。
- 不使用 JSX（Desktop 不编译插件里的 JSX），全部用 `jsx()/jsxs()` 手写。
- 只从 `@hermes/plugin-sdk` 与 `react` / `react/jsx-runtime` 导入；这两个是宿主
  注入的单例，不是 npm 依赖。
- 语法自检（不启动 Electron）：

  ```powershell
  Copy-Item .\plugin.js $env:TEMP\hfk.mjs -Force
  node --check $env:TEMP\hfk.mjs
  ```

## 验证状态（诚实说明）

**已在真实 Hermes Desktop 0.23.1 渲染进程里跑通**（2026-11-16，本机）：

| 已验证 | 证据 |
|---|---|
| 插件被发现并加载 | `设置 ▸ 插件` 列表出现 HFK，来源标 `Disk` |
| `register(ctx)` 不抛异常 | 侧栏出现 🛡 HFK 入口，主页面可打开 |
| 令牌**自动发现**生效 | 状态格显示「HFK 版本 0.1.0 · service.json 自动发现」 |
| 真实数据打通 | 六格全部真值：`0.23.1`、`精确支持`、`Gateway 运行中 · pid 27432 · 活跃 Agent 0`、`未应用 · 历史事务 1 个`、`可读` |
| 预设卡片渲染正确 | 保守「改 3 项」、平衡「改 3 项」，逐条 `当前 → 目标`（`60 → 120`、`true → false`、`（未设置）→ 5`） |
| 静态检查 | `node --check` 通过；22 个 `@hermes/plugin-sdk` 符号逐一对照 `apps/desktop/src/sdk/index.ts` |
| 数据层真实链路 | `plugin.clienttest.mjs` 对真实服务跑 28 项断言全过（含无令牌 401） |

仍然**未经**真实点击验证的部分：

- 「预览差异 / 应用 / 恢复 / 卸载」四个按钮的**交互结果**（HTTP 层已验证，按钮接线未点过）
- 逐项配置区里 `Switch` / `Checkbox` / `Select` / `IntEditor` 的实际手感
- 页面下半部分（逐项配置、补丁管理、历史页签）的完整渲染

若某个控件在你的构建上行为不符，改动点集中在
`EnumEditor` / `BoolEditor` / `IntEditor` / `TextEditor` 四个小组件。

## 其他限制

- 插件只驱动预览/应用/恢复/卸载；**不提供**逐事务选择恢复的 UI（CLI 支持
  `hfk restore --id <tx>`）。
