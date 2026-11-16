# HFK 安全模型

本文说明 HFK 0.1.0 的安全假设、风险分级、失败处理、明确的“不做”清单、本地服务的威胁面，以及每条保证对应哪个自动化测试。文中“代码中”的引用均指本仓库当前实现；“Hermes 中”的引用指本机安装的 Hermes 0.23.1。

## 1. 安全目标与假设

**目标**：让“降低 Hermes 审批打扰”这件事变成一次可预览、可回退、字节级可审计的字段修改，而不是直接编辑 `config.yaml` 或调用无校验的 `hermes config set`。

**信任假设**：

- 运行 HFK 的用户就是 Hermes 的属主，且本机没有恶意同权限进程（本地服务的安全边界是“同一用户”）。
- Hermes 0.23.1 的代码（`tools/approval*.py`、`hermes_cli/config*.py`、`hermes_yaml.py`）是可信基线；HFK 只往它的配置文件里写已核实存在的键。
- 用户对自己选择的预设/字段负责——`freedom` 与 B 类字段的后果由用户显式承担。

**不假设**：

- 不假设 Hermes 的配置写入器有跨进程锁（实测没有：只有进程内 `threading.RLock`）；
- 不假设 `hermes config set` 会做 schema 校验（实测接受任意点分键）；
- 不假设 `PUT /api/config` 能删除键（它把请求体 deep-merge 进现有配置，从不删除未提交的键）。

## 2. 三类风险分级

字段清单（`hfk/schema.py::CATALOGUE`）只收录“在 Hermes 0.23.1 源码中核实存在”的键。风险级别回答的是**“写错/被滥用会怎样”**，不是“这个键是否常用”。

### 2.1 A 类：普通、可恢复

判定标准：只改变“要不要问一次 / 等多久 / 在无人工通道时默认怎么办”，不改变代码级硬底线；任何时刻可按字段还原。

| 字段 | 分级理由 |
|---|---|
| `approvals.mode` | 决定审批主路径（manual/smart/off）。即使 `off`，HARDLINE 硬阻断与 `approvals.deny` 仍在（见第 4 节），因此是“放宽打扰”而非“拆掉底线”；可随时改回。 |
| `approvals.timeout` | 只影响提示等待时长；超时按 fail-closed（视为拒绝），不会因为调大而变成放行。 |
| `approvals.cron_mode` | 只作用于“本来就没有人在等审批”的 Cron 场景；默认 `deny` 是安全侧，改成 `approve` 需要显式选 `freedom`（有风险确认）。这是 A 类里影响面最大的一组之一，风险提示由预设的 `risk_note` 承担。 |
| `approvals.single_query_mode` | 同上，作用于 `-q`/`-Q` 单次查询会话（没有用户可回答审批）。 |
| `approvals.unattended_mode` | 同上，作用于 webhook / api_server 等没有 `/approve` 通道的平台。**这类平台完全没有人工把关通道**，`approve` 等于完全无人值守，因此只在 `freedom` 中出现且必须 `--acknowledge-risk`。 |
| `approvals.destructive_slash_confirm` | 纯交互确认（`/clear` `/new` `/reset` `/undo`）；关掉只减少一次点击，不改变危险命令的执行判定。 |
| `approvals.mcp_reload_confirm` | 纯交互确认（`/reload-mcp` 会让 prompt 缓存失效）；关掉只影响体验。 |
| `approvals.discord_prompt_timeout` | 只控制 Discord 审批按钮的可点击时长；不是任何预设的成员。 |
| `approvals.denial_breaker_threshold` | 控制辅助模型连续 DENY 后升级为硬停止的阈值；`0` 会关闭熔断（`freedom` 取 0），但硬阻断与 `deny` 规则不受影响，且该值随时可还原。 |
| `memory.write_approval` | 默认 `false`；改成 `true` 是**加强**审批，不是削弱。 |
| `skills.write_approval` | 同上，改成 `true` 是加强技能写入审批。 |

### 2.2 B 类：高风险但可由用户管理

判定标准：会削弱某一类检测、或扩大无人把关的执行面；**不进入任何预设**、默认不写、写入前预览必须给出显式警告，只能由用户逐项 `--set` 选择。

| 字段 | 分级理由 | 写入后的后果 |
|---|---|---|
| `command_allowlist` | Hermes 把通过 “always” 审批的**危险命令类别标签**记进这里（实测是 `recursive delete` 这类类别名，不是命令原文）。命中类别后，该类检测**永久跳过**。 | 等于给整类命令发永久通行证；要恢复必须把条目删掉（HFK 的预设永远不会自动加它）。 |
| `approvals.smart_policy` | 会被追加到智能审批“守卫模型”的 system prompt 末尾。规则写反/被注入会直接改变守卫判断。 | 可能让守卫更容易放行，且表现为“模型判断”而非显式规则，不易察觉。 |
| `approvals.deny` | 用户自己的 fnmatch 黑名单，**在 `--yolo` / `mode=off` 下仍然生效**；删除条目等于削弱最后一道闸。 | 新增是加强，删减是削弱；HFK 不提供“批量清空”的快捷入口。 |
| `approvals.approvers` | 决定谁能在聊天平台回答审批。`["*"]` = 任何已入群成员都可批准。 | 放宽到 `*` 等于把批准权交给群里任何人。 |
| `delegation.subagent_auto_approve` | 子代理执行工具调用时免审批；会显著扩大无人把关的执行面。 | 主代理的审批约束被子代理绕过。 |
| `hooks_auto_accept` | 免 TTY 提示直接接受新注册的 shell hook；hook 会执行真实命令。 | 新的 hook 注册不再需要人工确认，等于把“注册即执行”自动化。 |

补充说明（代码事实）：

- B 类的警告由 `build_plan` 生成，出现在 `preview` 的 `⚠` 行与 `--json` 的 `plan.warnings` 中；`apply` 的**文本输出不重复打印**这些警告，所以写 B 类字段前应先 `preview`。
- 风险确认参数 `--acknowledge-risk` **只门控 `freedom` 预设**，不门控 B 类字段；B 类的护栏是“不在预设里 + 预览警告 + 只能显式 `--set`”，不是强制二次确认。

### 2.3 C 类：HFK 永不写入

判定标准：要么是秘密、要么是“改错会让审批整体失效且难以察觉”的企业级边界、要么根本不存在于配置中。`hfk/schema.py::BLOCKED_FIELDS` 显式列出，UI 只解释“为什么不能改”。

| 字段/键 | 不写的原因 |
|---|---|
| `security.approval.transport` | 审批传输通道属于企业级安全边界；改错会让审批整体失效，且异常可能很隐蔽。0.1.0 不提供修改。 |
| `auxiliary.approval.api_key` | 辅助审批模型的 API Key，属于秘密。HFK 不读取、不写入、不记录任何秘密。 |
| `auxiliary.approval.provider` | 更换辅助审批模型提供商会改变“谁来判定低风险”，涉及外部服务与凭据，超出 0.1.0 范围。 |
| **任何不在 `CATALOGUE` 的键** | `validate_value` 对白名单外路径一律返回“不在已验证字段清单内，拒绝写入”；`apply` 遇到 `plan.errors` 会整体拒绝。 |

### 2.4 为什么 `fail_closed_when_no_human` 被拒绝

- 它**不是**配置键：它是 `tools/approval.py` 里 `check_approval(...)` 的一个函数关键字参数（默认 `False`），由调用方按场景传入；`hermes_cli/config_defaults.py` 的 `DEFAULT_CONFIG` 里不存在这个路径，Hermes 也不会从 `config.yaml` 读它。
- 因此把它写进 `config.yaml` 是“往文件里塞一个永远不会被读取的键”：既不会产生用户期待的效果，又会污染配置。
- HFK 的拒绝方式不是特判，而是白名单机制的自然结果：`fail_closed_when_no_human` 不在 `CATALOGUE`、也不在 `BLOCKED_FIELDS`，`validate_value` 会归类为“白名单外字段”并拒绝；`preview` 会把该字段标 `✘`，`apply` 直接拒绝执行。
- 结论：原项目规格里“通过配置开关 `fail_closed_when_no_human` 强制 fail-closed”的假设在 0.23.1 上不成立；HFK 不实现它，也不假装实现。

## 3. 代码级安全底线（不可被本工具削弱）

这些不是配置项，而是 Hermes 代码里的判定，**不存在任何 `config.yaml` 开关**能关掉它们，因此 HFK 在架构上就无法削弱：

| 底线 | 位置 | 作用 |
|---|---|---|
| `HARDLINE_PATTERNS` | 定义在 `tools/approval_detection.py:124`，由 `tools/approval_floors.py` 的 pre-gate floor 在 yolo / `mode=off` 之前执行 | 不可恢复系统破坏类命令的硬阻断 |
| 凭据检测 / 凭据上传检测 | `tools/approval_detection.py`（`_CREDENTIAL_*` 规则与 `_CREDENTIAL_UPLOAD_DESCRIPTION`） | 防止把密钥、Token 读出或外传 |
| Runtime Self-Delete Protection | `agent/runtime_self_protection.py::command_deletes_runtime`，在 `tools/approval.py` 的硬阻断路径执行（先于 yolo） | 防止 Agent 删除自身运行时 |
| Sudo Password Stdin Guard | `tools/approval_detection.py::_check_sudo_stdin_guard`，经 `tools/approval_floors.py::_sudo_stdin_block_result` 执行 | 防止通过 stdin 喂密码 |
| 审批上下文隔离 | session/turn/tool_call 的 contextvars 在 `tools/approval_context.py`；`_YOLO_MODE_FROZEN` 在 `tools/approval.py:48` | 跨会话/轮次/工具调用隔离审批上下文 |

`hfk catalogue` 里的 “代码级安全底线” 一节就是这张表的简化展示版本：它把 HARDLINE 的执行文件写成 `tools/approval_floors.py`、把 Runtime Self-Delete 写成 `tools/approval_detection.py`；上表是逐项核对 Hermes 0.23.1 源码后的**权威位置**。

## 4. 即使 `approvals.mode: off`（`--yolo`）仍然生效的东西

很多人把 `off` 理解成“什么都不拦”，这在 Hermes 0.23.1 上不成立。Hermes 的代码注释明确写着 yolo 只跳过**可恢复的审批层**，以下仍然阻断：

1. **HARDLINE 硬阻断**：灾难性/不可恢复系统破坏的命令模式，在进入 yolo 判定之前就拦下。
2. **`approvals.deny`**：用户自己的 glob 黑名单，注释写明 “a match blocks even under --yolo / mode=off”。
3. **凭据检测、sudo stdin 守卫、运行时自删除保护**等代码级检测（同上表）。
4. **永久放行清单（`command_allowlist`）反而是“跳过检测”的方向**：它不属于保护，属于用户主动开的口子——这正是它被归为 B 类、永不进预设的原因。

HFK 能做的只是“让你选择要不要保留提示”；它**无法**通过任何字段关掉上表这些代码级判定，文档与 UI 都不承诺“完全自由”。

## 5. 失败处理

### 5.1 Apply 的失败路径

| 失败点 | 行为 | 配置文件的最终状态 |
|---|---|---|
| 版本不受支持 / 配置不可读 | `_assert_writable` 抛错，不写 | 未变 |
| 计划含错误（未知字段、非法值、父级标量） | 抛 `ConfigValidationError`，不写 | 未变 |
| 计划没有任何可写字段 | 抛 `ConfigValidationError`（“没有需要修改的字段”） | 未变 |
| 预览后文件被改动（SHA-256 不一致） | 抛 `SignatureError`，不写 | 未变（除非改动是别人做的） |
| 备份创建/校验失败 | 抛 `BackupError`，不覆盖 | 未变 |
| 写入抛异常 | 用备份**原始字节**原子回滚 + 记 `failed` 事务 + 抛 `ConfigWriteError` | 字节级还原 |
| 写后回读校验不一致 | 同上（回滚 + `failed` 事务 + 抛错） | 字节级还原 |
| 事务记录追加失败 | 异常向上抛；**配置已写入，不回滚** | 已修改（备份在 `backups/`，见 8.3 残余风险） |

“回滚”的实现是 `atomic_write_bytes(config_path, before_bytes)`：直接把备份字节原子写回，不重新序列化，因此是真正字节级还原，而不是“重新生成一份看起来一样的 YAML”。

### 5.2 备份校验

- 每次 Apply 前：把 `config.yaml` 原始字节复制到 `backups/config-<时间戳>-<hex>.yaml`（`open(..., "xb")` 排他创建 + `fsync`），随后回读并与源文件 SHA-256 比对；不一致就抛 `BackupError`，**不继续覆盖**。
- 使用备份回滚前：`read_backup_bytes` 会重新调用 `verify_backup`（存在性 + 哈希）；损坏/缺失则拒绝使用。
- 注意：`restore` **不读取备份文件**来恢复字段（它依据事务里记录的字段级 `before`/`after`），所以“备份损坏”不会让字段级恢复写坏配置；但备份是整文件回退的最后手段，损坏/缺失就意味着失去该手段。

### 5.3 并发修改拒绝

- 预览时记录 `config.yaml` 的 SHA-256（`expected_sha256`），写入前重新计算并比对；不一致 → `SignatureError`，不写。
- 同时，写入是“同目录临时文件 + `os.replace`”的原子替换，读方要么看到旧文件、要么看到新文件。
- 但这不是锁：检查与替换之间仍有极小的 TOCTOU 窗口（见 8.3）。

### 5.4 Restore 的失败处理

| 情况 | 行为 |
|---|---|
| 事务不是 pending（已恢复/失败）或 ID 不存在 | 抛 `ConfigValidationError`，不写 |
| 版本不受支持 / 配置不可读 | 抛错，不写 |
| 备份文件缺失 | 该事务跳过并警告，**保持 pending**，不删除记录 |
| 某字段当前值 ≠ HFK 写入值 | 保留用户值，记冲突，事务标记 `partially_restored` |
| 某字段现在不存在且 HFK 写过值 | 保留删除状态，记冲突 |
| 写入失败 | 该事务保持 pending，报告“可重试” |
| 恢复写入成功但事务状态更新失败 | 字段已恢复，事务记录可能仍是旧状态（见 8.3） |

多事务恢复按事务逆序执行，但**不是整批一个原子操作**：每条事务单独一次原子写，中途失败时已完成的事务不会回退。

### 5.5 “未完成”绝不记成“完成”

- `Transaction.is_pending` 只认 `applied`；`failed` / `restored` / `partially_restored` 都不会被当成“还要恢复”。
- 写入失败时 `_record_failure` 记录 `status=failed`、原始/当前哈希、备份路径和失败原因（记录失败本身不会掩盖原始异常）。
- 单条事务记录损坏时，加载会跳过它但保留文件原样，避免“以为恢复了其实记录丢了”。

## 6. “Never” 清单（明确的负向承诺）

1. **不读、不写、不记录秘密**：不主动提取 API Key / Token / 密码字段；`auxiliary.approval.api_key` 显式排除。CLI 不打印秘密；服务日志只记路由、错误码、事务 ID、字段计数。*说明*：解析 YAML 时整份文档必然存在于进程内存（解析器行为），HFK 不会把其中的秘密字段输出到 stdout、日志或 HTTP 响应。
2. **没有任意命令执行能力**：本地服务路由是固定白名单，没有 exec/shell 端点；HFK 自身只在环境探测时调用 `git describe/rev-parse/status` 与 `node --version`（固定 argv、有超时、失败即忽略），不接受任何用户命令字符串。
3. **不整文件覆盖用户配置**：补丁只做字段级写入；“整文件写”只出现在两处——写入前的字节级回滚、以及用户手工用备份回退。
4. **不删除用户数据**：不删配置、SOUL.md、Skills、记忆、会话数据库；`uninstall` 只删除 `<HERMES_HOME>\desktop-plugins\hfk` 与写卸载记录，保留全部备份与事务历史。
5. **不修改 Hermes 安装目录/源码**：只读探测（`install-stamp.json`、`gateway.pid`、`gateway_state.json`、`apps/desktop/package.json`、git 元数据）。
6. **不重启、不结束 Hermes 网关**：HFK 只写文件；Hermes 按文件签名缓存配置、守卫每次读 `load_config_readonly()`，写文件后下一次检查即生效。
7. **不写白名单之外的键**：包括 `auxiliary.approval.*`、`security.approval.*`、`fail_closed_when_no_human` 以及任何猜测出来的键。
8. **不自作主张写入**：`apply`/`restore`/`uninstall` 只会由显式 CLI 命令或显式 HTTP 调用触发；服务不会在后台自动改写配置。
9. **不发起外部网络请求**：核心与 CLI 不访问互联网；唯一的网络面是绑定 `127.0.0.1` 的本地服务。

## 7. 本地服务的威胁说明

| 面 | 设计 | 说明/残余风险 |
|---|---|---|
| 绑定地址 | 只绑 `127.0.0.1` | 局域网/公网无法直连；但同机进程可访问 |
| 认证 | 每次启动随机 `secrets.token_urlsafe(32)`，请求头 `X-HFK-Token`，`compare_digest` 常数时间比较 | 令牌写在本机 `hfk/service.json`（0600）；**同一用户权限的进程可以读取它**——安全边界是“同一用户” |
| 令牌泄漏面 | 不写日志、不在 HTTP 响应中回显；`--print-token` 是用户显式选择 | 日志文件 `hfk/logs/hfk.log` 只记录路由/错误/事务 ID |
| `/api/health` | **无需令牌**，只返回 `{"ok": true, "hfk_version": …}` | 任何本机进程可探测服务是否存在；不泄漏配置 |
| DNS rebinding | 校验 `Host` 头必须是 `127.0.0.1`/`localhost`/`::1`，否则 403 | 阻止浏览器页面借 DNS rebinding 访问本地服务 |
| CORS | 允许 `Origin: null` / `file://`（Electron renderer）与 `http://127.0.0.1*` / `localhost*` | 本地 HTML 文件理论上可发起请求，但仍需令牌 |
| 命令执行 | 无 exec 端点；路由固定 | 即使令牌泄漏，也只能执行 HFK 的固定视图/补丁动作（补丁动作本身仍有版本/白名单/CAS/备份保护） |
| 请求体 | 上限 1 MiB | 只接受字段路径与标量值，拒绝超大请求 |
| 传输加密 | 无 TLS | 仅回环；同机抓包需要额外权限 |
| 拒绝服务 | 无速率限制（`ThreadingHTTPServer`） | 同机同用户进程可刷请求；影响限于 HFK 自身操作，且这些操作仍需通过引擎保护 |

Desktop 插件（`<HERMES_HOME>\desktop-plugins\hfk\plugin.js`，无 manifest 文件，不属于 Python 包）只做 UI：它从 `service.json` 读取地址与令牌、带 `X-HFK-Token` 调本地服务；它自身不做配置写入。插件 UI 的安全依赖 core 服务的上述约束。

## 8. 残余风险与已知缺口

### 8.1 没有跨进程锁

Hermes 的 `config.yaml` 写入路径没有跨进程文件锁（只有进程内 `threading.RLock`），HFK 也没有引入锁文件。缓解手段：

- 严格字段白名单（写入面极小）；
- 预览时绑定 SHA-256、写入前比对（CAS）；
- 原子替换；
- 字节级备份 + 失败回滚。

**残余**：CAS 检查与 `os.replace` 之间存在 TOCTOU 窗口。如果另一个进程恰好在两者之间改写配置，HFK 会覆盖那次改写（随后的回读校验只验证 HFK 写入的字段，不检测其它字段是否被改动）。目前没有自动化测试能覆盖这个竞态。

### 8.2 Windows 重命名争用的降级

Windows 上 `os.replace` 可能因句柄占用失败（`WinError 5/32/33`）。HFK 做 6 次退避重试；仍失败时退回“原地重写”（与 Hermes 行为一致的取舍：宁可非原子，也不留下写不进去的补丁）。原地重写期间进程崩溃可能留下不完整文件——此时需要用 `backups/` 里的字节级原件手工回退。

### 8.3 事务记录与配置写入不是同一原子操作

- 配置写入成功后，`TransactionStore.append` 才写 `transactions.json`；若该步失败，配置已修改但事务未记录，`restore` 找不到这次 Apply。备份文件仍在 `backups/`，可手工回退。
- `restore` 成功后更新事务状态失败时，字段已恢复但事务可能仍显示 pending。

这两处是 0.1.0 的已知缺口，已在本文件列出；修复方向是把“配置 + 事务”做成可恢复的两阶段提交（未实现）。

### 8.4 同族版本降级放行

`0.23.x` 同族版本 `supported=True, exact=False`——允许写入但字段未逐版本复核。这是“可用性 vs 严格性”的取舍；安全侧做法是只在 0.23.1 上 Apply，同族版本先 `preview` 并人工确认。

### 8.5 `--set` 可绕过风险确认

`--acknowledge-risk` 只门控 `freedom` 预设；B 类字段可以通过 `--set` 直接写，只有预览警告。这是“逐项显式选择”的设计后果，不是漏洞，但用户必须知道。

### 8.6 备份的可用性依赖用户

`restore` 不校验备份内容（用事务里的字段级数据），所以备份损坏不会阻断字段级恢复；但整文件回退依赖备份文件本身。删除 `hfk/backups/` 会同时失去审计证据与最后手段。

## 9. 保证 ↔ 测试对应表

以下测试均可用 `python -m unittest discover -s tests -v`（43 个）与 `python -m hfk.cli selftest`（6 个场景、29 项检查）复现；全部在临时沙箱执行，不触碰真实配置。

| 安全保证 | 证明它的测试 |
|---|---|
| 只写白名单字段；未知键拒绝 | `tests/test_schema.py::TestSchema::test_validate_rejects_unknown_field`、`test_blocked_fields_are_rejected`、`test_catalogue_paths_unique` |
| 字段清单不含秘密键 | `tests/test_schema.py::TestSchema::test_no_secret_fields_in_catalogue`（断言路径中无 `api_key`/`token`/`password`） |
| 每个字段都有源码证据与作用说明 | `tests/test_schema.py::TestSchema::test_every_spec_has_evidence` |
| 枚举/默认值以 0.23.1 为准（`mode` 默认是 `smart` 不是 `manual`） | `tests/test_schema.py::TestSchema::test_enum_values_are_authoritative`、`test_mode_default_is_smart_not_manual` |
| 类型校验（bool 不能冒充 int、非法枚举拒绝） | `tests/test_schema.py::TestSchema::test_validate_rejects_bool_as_int`、`test_validate_rejects_wrong_enum` |
| 预设永不含 B 类字段；预设取值合法 | `tests/test_schema.py::TestPresets::test_presets_never_include_b_class`、`test_each_preset_entry_is_a_legal_value` |
| 默认预设是 balanced；freedom 需确认 | `tests/test_schema.py::TestPresets::test_default_preset_is_balanced`、`test_freedom_requires_acknowledgement` |
| 兼容清单与字段清单完全一致 | `tests/test_schema.py::TestManifestCoverage::test_manifest_matches_catalogue` |
| 版本判定：精确支持 / 同族降级 / 其他只读 / 无法识别只读 | `TestManifestCoverage::test_exact_version_supported`、`test_same_family_supported_with_warning`、`test_other_version_unsupported`、`test_unknown_version_unsupported` |
| YAML 1.1：`off` 必须加引号且读回字符串 | `tests/test_hfk.py::TestYamlIO::test_off_is_quoted_and_reads_back_as_string`；selftest 场景 1 |
| 注释与 CRLF 保留 | `TestYamlIO::test_comments_and_crlf_preserved`；selftest 场景 1 |
| 只改目标字段；无变化时文件字节不变 | `TestYamlIO::test_unchanged_write_is_stable`；开发期 `scripts/parity_probe.py`（字节一致 + 1 行改动） |
| 能删除 HFK 新建的键 | `TestYamlIO::test_creates_missing_field_then_deletes` |
| Apply → Restore 字节级保真 | `TestEngine::test_apply_restore_is_byte_exact`；selftest 场景 2 |
| 目标值已生效时拒绝空 Apply | `TestEngine::test_no_change_raises` |
| 冲突时保留用户当前值，事务标记部分恢复 | `TestEngine::test_conflict_preserves_user_value`；selftest 场景 3 |
| 多次 Apply 按事务逆序恢复 | `TestEngine::test_multiple_applies_restore_in_reverse`；selftest 场景 4 |
| 预览后文件变化 → 拒绝写入（CAS） | `TestEngine::test_signature_mismatch_blocks_apply` |
| 未验证版本禁止 Apply | `TestEngine::test_unsupported_version_blocked`；selftest 场景 5 |
| 写后校验失败 → 回滚原始字节 + 记 `failed` 事务 + 保留备份 | `TestEngine::test_rollback_on_verification_failure` |
| 父级是标量时拒绝写入（不静默丢值） | `TestEngine::test_refuses_non_mapping_parent` |
| 备份哈希与原文件一致 | `TestEngine::test_backup_hash_matches_original` |
| 卸载保留备份、恢复配置、写卸载记录 | `TestEngine::test_uninstall_keeps_backups`；selftest 场景 6 |
| 本地服务要求令牌；无令牌 401；正确令牌可 apply | `tests/test_hfk.py::TestService::test_requires_token_and_applies` |
| 点分键路径与字面点键语义（不会写幻影节点） | `tests/test_hfk.py::TestKeyPath` 全部 6 个测试 |

### 9.1 代码已实现但**没有**自动化测试覆盖的保证

诚实列出（这些靠代码审查与手工验证，不是测试结论）：

- `Host` 头回环校验（403 `bad_host`）——`TestService` 只覆盖 health/401/apply，未发非回环 `Host` 请求。
- 服务令牌文件 `service.json` 的 0600 权限与实际写入——未在测试中断言。
- 卸载时插件目录的 `assert_within(home)` 路径越界保护——未单独测试。
- `freedom` 的 `--acknowledge-risk` 在 `facade.apply` 里的强制逻辑——测试只断言了 `PRESETS["freedom"].requires_acknowledgement`，没有测试“不带参就抛错”。
- 备份创建后损坏时 Apply 拒绝继续（`create_backup`/`verify_backup` 的失败分支）——未构造损坏场景。
- 事务文件写入失败后的行为（8.3）——未测试，属于已知缺口。
- Windows 重命名争用重试与原地重写降级——未测试（依赖真实文件占用）。
- 跨进程并发写入的 TOCTOU（8.1）——无法用当前测试框架稳定复现，未覆盖。
- 真实 Hermes 上的热加载效果（写文件后审批行为改变）——属于 Hermes 侧行为，HFK 测试不覆盖；依据是 Hermes 源码的缓存签名与 `load_config_readonly()` 调用路径。
