# HFK（Hermes Freedom Kit）0.1.0

HFK 是一个针对 **Hermes Agent 0.23.1** 的 Windows 宿主补丁工具：它扫描本机的 Hermes 安装与 `config.yaml`，把「审批要不要问、超时多久、无人值守场景拦不拦」这类**已经核实存在**的配置项，以字段级、可预览、可逆、可审计的方式写入，并在写入前强制做字节级备份、写入后做回读校验——它不修改 Hermes 源码、不注入进程、不读取任何密钥，也不做整文件覆盖；之所以需要它，是因为 Hermes 自带的 `config.yaml` 写入路径既没有跨进程文件锁、`hermes config set` 也不做 schema 校验，直接用脚本改配置很容易在网关并发写、YAML 1.1 的 `off`/`no` 语义、或整份文档重排上踩坑，HFK 把这些坑固化成一条受控写入路径（字段白名单 → SHA-256 前置比对 → 备份 → 单次 ruamel 原子写 → 回读校验 → 事务记录）。

> 本文档描述的是**当前代码的真实行为**。字段清单与默认值以 `hfk/schema.py` 和 Hermes 0.23.1 的 `hermes_cli/config_defaults.py` 为准。

## 目标环境（已核对）

| 项 | 值 |
|---|---|
| Hermes Agent | **0.23.1**（git tag `v0.23.1`，commit `3b421578588243f920886e6fe79a6faa87ea026e`） |
| Hermes Home（本机） | `<HERMES_HOME>` |
| Windows 默认 Home | `%LOCALAPPDATA%\hermes`（**不是** `%USERPROFILE%\.hermes`，后者是 POSIX 默认值） |
| 配置文件 | `<HERMES_HOME>\config.yaml`，UTF-8、CRLF、无 BOM；本机 18,800 字节 / 714 行 |
| 运行环境 | Python 3.11+，`ruamel.yaml` 0.18.x |
| 数据目录 | `<HERMES_HOME>\hfk\`（备份、事务、日志、服务令牌） |

版本号来源按优先级为 `install-stamp.json` → 安装目录 git 标签 → `gateway_state.json`。`apps/desktop/package.json` 里写的是占位值 `0.0.0`，HFK 刻意忽略它，并会在 `env` 里把这件事作为提示打印出来。

## 60 秒快速开始

在 PowerShell 中（本机已有带 `ruamel.yaml` 0.18.17 的 Hermes venv）：

```powershell
cd <WORKSPACE>\Hermes-Freedom-Kit
$env:PYTHONUTF8 = "1"      # 默认 GBK 代码页下，输出里的 ✔/⚠ 会触发 UnicodeEncodeError
$py = "<HERMES_VENV_PYTHON>"

# 1) 检测：Hermes 在哪、什么版本、配置可读吗
& $py -m hfk.cli env

# 2) 预览：只读，展示每个字段「当前值 → 目标值」与实际会写几项
& $py -m hfk.cli preview --preset balanced

# 3) 应用：自动备份 + 事务记录（默认写入真实 %LOCALAPPDATA%\hermes\config.yaml）
& $py -m hfk.cli apply --preset balanced

# 4) 恢复：字段级回退；用户后来改过的字段会被保留并报冲突
& $py -m hfk.cli restore
```

只想先看、不想写：前两步就够了（`env` / `preview` / `status` / `catalogue` / `presets` / `transactions` / `backups` 全部只读）。不确定时加 `--json` 便于脚本消费，注意全局参数必须放在子命令**前面**：

```powershell
& $py -m hfk.cli --json status
```

## 已验证字段一览

默认预设 `balanced`。风险分级：**A** = 普通、可恢复；**B** = 高风险但可由用户管理（**任何预设都不包含**，只能逐项 `--set`，预览会给出 ⚠）；**C** = HFK 永不写入（见 `catalogue`）。

| 路径 | 作用 | 类型 | 默认 | 风险 |
|---|---|---|---|---|
| `approvals.mode` | 审批主模式：manual 逐条问 / smart 辅助模型放行低风险 / off（等同 `--yolo`） | enum（manual / smart / off） | `smart` | A |
| `approvals.timeout` | 审批等待超时（秒），超时按 fail-closed 处理（视为拒绝） | int 1…3600 | `300` | A |
| `approvals.cron_mode` | 定时（Cron）任务遇到危险命令：deny 立刻拦下 / approve 自动放行 | enum（deny / approve） | `deny` | A |
| `approvals.single_query_mode` | `-q`/`-Q` 单次查询会话没有人在等审批时的策略 | enum（deny / approve） | `deny` | A |
| `approvals.unattended_mode` | webhook / api_server 等无「回答审批」通道的平台策略 | enum（deny / approve） | `deny` | A |
| `approvals.destructive_slash_confirm` | `/clear` `/new` `/reset` `/undo` 等会丢弃会话状态的命令是否二次确认 | bool | `true` | A |
| `approvals.mcp_reload_confirm` | `/reload-mcp` 重建 MCP 工具集前是否确认 | bool | `true` | A |
| `approvals.discord_prompt_timeout` | Discord 审批按钮可点击时长（秒），最短 30 秒、上限受 `MAX_SAFE_TIMEOUT_S` 约束 | int 1…3600 | `300` | A |
| `approvals.denial_breaker_threshold` | 辅助模型连续拒绝多少次后升级为硬停止；0 表示关闭熔断 | int 0…50 | `3` | A |
| `command_allowlist` | **永久放行的危险命令「类别标签」**（不是命令原文）；命中即永久跳过该类检测 | list[str] | `[]` | B |
| `approvals.smart_policy` | 追加到智能审批守卫模型 system prompt 末尾的额外规则 | str | `""` | B |
| `approvals.deny` | 强制阻断规则（命令 glob）；**即使 `mode=off` / `--yolo` 也照样拦** | list[str] | `[]` | B |
| `approvals.approvers` | 谁可以回答聊天审批：`["*"]` 任何已入群成员 | list[str] | `["*"]` | B |
| `delegation.subagent_auto_approve` | 子代理执行工具调用时是否免审批（会扩大无人把关的执行面） | bool | `false` | B |
| `hooks_auto_accept` | 是否免 TTY 提示直接接受新注册的 shell hook（hook 会执行真实命令） | bool | `false` | B |
| `memory.write_approval` | Agent 写入长期记忆前是否需要你确认 | bool | `false` | A |
| `skills.write_approval` | Agent 写入/修改技能文件前是否需要你确认 | bool | `false` | A |

预设与目录的关系：

| 预设 | 内容 | 需要确认 |
|---|---|---|
| `conservative` | 审批改回 `manual`、非交互场景全 deny、二次确认全开、超时 300、熔断 3 | 否 |
| `balanced`（默认） | 保留 smart 与全部 deny 策略，只关掉 `/clear`、`/reload-mcp` 的重复确认，超时收紧到 120、熔断 5 | 否 |
| `freedom` | `mode: off` + cron / 单次查询 / 无人值守全部 approve、超时 60、熔断关闭 | **是**（`--acknowledge-risk`） |
| `custom` | 不带任何预设字段，只应用你 `--set` 的项（空 `custom` 会因「没有需要修改的字段」被拒绝） | 否 |

预设只写 A 类字段；某个字段**当前缺失**且目标值恰好等于 Hermes 默认值时，HFK 不写它（避免把 `config.yaml` 弄脏），预览里会标注「当前未设置，目标值等于 Hermes 默认值，无需写入」。

## HFK 不会碰的东西

- **任何秘密**：API Key、Token、密码一律不读、不写、不记日志；`auxiliary.approval.api_key` 与 `auxiliary.approval.provider` 在显式排除表中，整个 `auxiliary.approval.*` 子树也不在字段白名单内。
- **Hermes 源码与安装目录**：只读探测，不写入。
- **模型 / provider / SOUL.md / Skills / 记忆 / 会话数据库**：不在白名单，不可能被本工具写入。
- **网关进程**：不重启、不杀进程；因为 Hermes 按文件签名缓存配置、审批守卫每次都调用 `load_config_readonly()`，写文件后下一次检查即生效（无需重启网关）。
- **其他 Profile**：只操作探测到的那一个 Home（可用 `--home` 指定）。
- **整文件覆盖**：补丁只做字段级写入；备份是对原文件的**字节复制**，不是覆盖。

## 项目结构

```text
Hermes-Freedom-Kit/
├─ hfk/
│  ├─ __init__.py            包说明（不导入 Hermes 任何模块）
│  ├─ version.py             HFK 0.1.0 / 事务 schema 版本
│  ├─ errors.py              异常体系（每类失败带 machine-readable code）
│  ├─ util.py                sha256、UTC 时间戳、原子 JSON、路径越界守卫
│  ├─ env.py                 宿主探测：Home/安装目录/版本/Gateway/配置可读性
│  ├─ schema.py              已验证字段清单、取值校验、A/B/C 风险分级
│  ├─ profiles/presets.py    四档预设、预设+逐项覆盖、差异说明
│  ├─ compat/__init__.py     版本兼容判定（精确 / 同族降级 / 只读）
│  ├─ compat/manifests/hermes-0.23.1.json   0.23.1 的验证清单
│  ├─ config/keypath.py      与 Hermes 一致的点分键路径语义
│  ├─ config/yaml_io.py      保注释 YAML 读写 + 原子替换（复刻 Hermes 发射器策略）
│  ├─ engine/backup.py       字节级备份、回读哈希校验
│  ├─ engine/transaction.py  字段级事务存储（transactions.json）
│  ├─ engine/patch.py        build_plan / apply / restore / uninstall / status
│  ├─ facade.py              CLI、本地服务、插件共用的高层接口
│  ├─ cli.py                 命令行入口（``python -m hfk.cli``）
│  ├─ service/app.py         本地 HTTP 服务（仅 127.0.0.1）
│  ├─ selftest.py            内置自检（6 个场景）
│  └─ testing.py             临时沙箱 Hermes 环境（测试专用）
├─ scripts/
│  ├─ parity_probe.py        开发期探针：与 Hermes 写入器做字节对照
│  └─ probe_schema.py        开发期探针：从 Hermes DEFAULT_CONFIG 导出字段
├─ tests/
│  ├─ test_hfk.py            引擎/服务端到端测试
│  └─ test_schema.py         字段清单/清单文件/预设一致性测试
├─ docs/
│  ├─ USER_GUIDE.md          安装、每个命令、冲突、卸载、排错、安全回退
│  ├─ ARCHITECTURE.md        模块、写路径、事务模型、兼容注册表、服务分层
│  └─ SAFETY_MODEL.md        风险分级理由、失败处理、威胁与测试映射
├─ pyproject.toml            包元数据（entry point：``hfk``）
└─ README.md
```

## 运行测试

全部测试都在**临时沙箱**里执行，不会读取或修改真实 `config.yaml`。

```powershell
$env:PYTHONUTF8 = "1"
$py = "<HERMES_VENV_PYTHON>"
cd <WORKSPACE>\Hermes-Freedom-Kit

# 43 个单元/集成测试（含一条与 Hermes 自身写入器的字节一致性对照）
& $py -m unittest discover -s tests -v

# 内置自检：6 个场景（YAML 语义、Apply/Restore 字节保真、冲突、多次 Apply 逆序、未知版本、卸载）
& $py -m hfk.cli selftest
```

没有现成 venv 时：

```powershell
py -3 -m venv .venv
.venv\Scripts\pip install "ruamel.yaml>=0.18.16,<0.20"
.venv\Scripts\python -m unittest discover -s tests -v
```

`scripts/parity_probe.py` 是**开发期探针**（不是单元测试）：它用 Hermes 自己的 `utils.atomic_roundtrip_yaml_update` 做对照，验证同一字段修改后两者产物字节一致、一次字段修改只变动 1 行、CRLF 与 `mode: "off"` 的 YAML 1.1 引号语义正确。脚本里的源文件路径是写死的，换机器需要先改。

## 兼容性声明

- **精确支持**：Hermes **0.23.1**——`hfk/compat/manifests/hermes-0.23.1.json` 列出的 17 个字段逐一在 0.23.1 上核对过（含 Hermes 默认值）。
- **同族降级**：`0.23.x`（`family: "0.23.x"`）允许运行，但 `exact=false`，字段未逐版本复核——应用前必须重新预览并自行确认差异。
- **其他版本**：只读。可以查看环境、字段目录、历史事务与备份，但 `apply` / `restore` / `uninstall` 会被拒绝（`unsupported_version`）。
- **无法确定版本**（install-stamp / git / gateway 都取不到）：同样只读，禁止写入。

## 已知限制

- **只精确验证 0.23.1**；0.23.x 是降级放行，其他版本只读（见上）。
- **Windows 优先**：路径处理、CRLF、`WinError 5/32/33` 重试都是按 Windows 写的；Linux / macOS 未验证。
- **写入面很窄**：只写 `hfk/schema.py` 白名单里的 17 个字段，别的键一律拒绝；bridge / 网关通道、`security.approval.transport` 等不在范围内。
- **`hooks_auto_accept` 及全部 B 类字段不在任何预设里**：要改必须 `--set` 显式写，并在预览里确认 ⚠；`freedom` 的 `--acknowledge-risk` 只门控 preset，不门控 `--set`。
- **没有跨进程文件锁**：走「预览时记录 SHA-256 → 应用前比对」（CAS）+ 原子替换 + Windows 重试；检查和替换之间仍有极小的 TOCTOU 窗口，无法做到绝对串行化。
- **`restore` 也要求版本受支持**：升级到未验证的 Hermes 版本后，HFK 不能替你回退；此时请用 `hfk/backups/` 里的字节级备份手工回退。
- **插件 UI 依赖 core service 运行**：插件只做展示/触发，真正的写入在 Python core；服务没起时插件不可用。服务只监听 `127.0.0.1`，且 `service` 命令不使用 `--home/--install-dir/--json`（总是自动探测 Home）。
- **默认 GBK 代码页的控制台**：CLI 的 `✔/✘/⚠` 输出会触发 `UnicodeEncodeError`，请先 `$env:PYTHONUTF8='1'`（或改用 `--json`）。

## 文档

- [用户指南](docs/USER_GUIDE.md)：安装、每个命令、预览怎么读、冲突怎么办、卸载保证、排错、安全回退步骤。
- [架构说明](docs/ARCHITECTURE.md)：模块职责、端到端写路径、事务/备份模型、兼容注册表、服务与插件分层、如何新增 Hermes 版本。
- [安全模型](docs/SAFETY_MODEL.md)：A/B/C 分级理由、`mode: off` 下仍生效的底线、失败处理、Never 列表、本地服务威胁说明、保证与测试的对应表。
