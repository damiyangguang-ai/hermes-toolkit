# HFK 用户指南

本指南覆盖 HFK 0.1.0 的安装、全部 CLI 命令、预设与逐项选择、预览/冲突的阅读方式、卸载保证、故障排查与安全回退步骤。

> **示例输出说明**：下文命令中的路径以 `<HERMES_HOME>` 表示你机器上的 Hermes Home（本机为 `<HERMES_HOME>`）。示例输出采自一个临时沙箱环境，路径做了占位替换、其余字段保持原样，仅用于展示输出**形状**；真实运行时的时间戳、事务 ID、哈希值都会不同。

## 1. 安装与准备

### 1.1 前置条件

| 条件 | 要求 |
|---|---|
| 操作系统 | Windows（HFK 按 Windows 的路径/换行/重命名争用写；Linux/macOS 未验证） |
| Python | 3.11 及以上 |
| 依赖 | `ruamel.yaml` 0.18.x（与 Hermes 0.23.1 使用同一 YAML 引擎） |
| Hermes | 0.23.1（精确支持；`0.23.x` 降级放行，其他版本只读） |

本机已有满足条件的解释器（内含 ruamel 0.18.17）：

```text
<HERMES_VENV_PYTHON>
```

### 1.2 三种运行方式

**A. 直接用 Hermes 的 venv（本机最省事）**

```powershell
cd <WORKSPACE>\Hermes-Freedom-Kit
$py = "<HERMES_VENV_PYTHON>"
& $py -m hfk.cli env
```

**B. 新建独立 venv**

```powershell
cd <WORKSPACE>\Hermes-Freedom-Kit
py -3 -m venv .venv
.venv\Scripts\pip install "ruamel.yaml>=0.18.16,<0.20"
.venv\Scripts\python -m hfk.cli env
```

**C. 安装成命令（获得 `hfk` 入口）**

```powershell
cd <WORKSPACE>\Hermes-Freedom-Kit
.venv\Scripts\pip install -e .
hfk env
```

`python -m hfk.cli` 与 `hfk` 的行为完全一致（后者只是 `pyproject.toml` 里的 entry point）。

### 1.3 全局参数：必须放在子命令前面

| 参数 | 作用 |
|---|---|
| `--home <PATH>` | 显式指定 Hermes Home（默认自动探测，顺序见下） |
| `--install-dir <PATH>` | 显式指定 Hermes 安装目录（默认从 gateway 命令行 / `<home>\hermes-agent` 探测） |
| `--json` | 输出 JSON，便于脚本或插件消费 |

```powershell
# 正确
& $py -m hfk.cli --json status
& $py -m hfk.cli --home D:\hermes-home env

# 错误：argparse 会报 unrecognized arguments: --json
& $py -m hfk.cli status --json
```

Home 自动探测顺序：显式 `--home` → 环境变量 `HERMES_HOME` → Windows 的 `%LOCALAPPDATA%\hermes`（支持 `HERMES_DATA_DIR_SUFFIX` 后缀）→ `~/.hermes`。候选目录必须至少含 `config.yaml`、`.env`、`state.db` 之一才会被接受。

> `service` 子命令**不使用** `--home/--install-dir/--json`：它内部固定调用自动探测。多 Home 场景请通过环境变量 `HERMES_HOME` 指定。

### 1.4 控制台编码（本机必读）

本机 PowerShell 的活动代码页是 **936（GBK）**，而 CLI 的状态输出包含 `✔` `✘` `⚠`。直接运行会抛：

```text
UnicodeEncodeError: 'gbk' codec can't encode character '\u2714' in position 15: illegal multibyte sequence
```

三种规避方式（任选其一）：

```powershell
$env:PYTHONUTF8 = "1"        # 推荐：仅影响当前会话
chcp 65001                    # 改用 UTF-8 代码页
& $py -m hfk.cli --json env   # JSON 输出不含 ✔ 等符号
```

`hfk selftest` 同样会打印 `✔`，所以运行自检前也必须先设置 `PYTHONUTF8`。

## 2. 命令参考

所有命令的写盘行为：

| 命令 | 是否写 `config.yaml` | 是否写 `hfk/` 数据目录 |
|---|---|---|
| `env` `status` `catalogue` `presets` `preview` `transactions` `backups` | 否 | 否（只读） |
| `apply` | 是（字段级） | 是（备份 + 事务） |
| `restore` | 是（字段级） | 是（更新事务状态） |
| `uninstall` | 间接（先 restore） | 是（卸载记录；保留备份/事务） |
| `selftest` | 否（临时沙箱） | 否（临时沙箱） |
| `service` | 否（由收到的 POST 决定） | 是（`service.json`、`hfk.log`） |

### 2.1 `env` — 环境报告

```powershell
& $py -m hfk.cli env
```

```text
Hermes Freedom Kit 0.1.0
==============================================================
Hermes 版本      : 0.23.1  （来源 install-stamp.json）
兼容性          : ✔ 支持 — 精确匹配已验证版本 0.23.1
Hermes Home     : <HERMES_HOME>
安装目录        : <HERMES_HOME>\hermes-agent
当前 Profile    : default
配置文件        : <HERMES_HOME>\config.yaml
配置可读        : 是 (18800 字节)
Gateway         : 运行中 pid 12345 [running]
Python / Node   : 3.11.15 / 22.22.2
模型供应商      : <provider> （<model>）
未完成事务      : 0
HFK 数据目录    : <HERMES_HOME>\hfk
Desktop 包版本  : 0.0.0（占位值，未用于版本判定）
--------------------------------------------------------------
提示：
  · apps/desktop/package.json 的版本号是 '0.0.0'（占位值），已按设计忽略，未作为 Agent 版本使用
  · 安装目录存在未提交的本地修改（git dirty）
```

关注点：

- `兼容性` 为 `✘ 不支持` 时，`apply/restore/uninstall` 都会被拒绝，只能查看。
- `配置可读` 为 `否` 时，`apply` 拒绝执行（会打印 `配置问题`）。
- `提示` 段不是错误：`git dirty`、Desktop 占位版本、网关记录与探测不一致都会在这里说明。

### 2.2 `status` — 补丁状态

```powershell
& $py -m hfk.cli status
```

```text
HFK 0.1.0 — 状态
--------------------------------------------------------------
Hermes 版本     : 0.23.1（支持）
Profile         : default
未完成事务      : 1
事务总数        : 1
备份数量        : 1
最近 Apply      : 2026-11-16T06:41:13+00:00
最近 Restore    : 无
生效中的字段：
  · approvals.timeout = 120
  · approvals.mcp_reload_confirm = False
  · approvals.denial_breaker_threshold = 5
```

说明：

- `未完成事务` = 状态为 `applied` 的事务数，即「HFK 写过但还没回退」的补丁。
- `生效中的字段` 来自这些未恢复事务记录，**不会**重新读取文件比对；如果你手工改过配置，这里仍显示 HFK 写入值。
- `--json` 键：`hfk_version`、`hermes_version`、`supported`、`compat_reason`、`profile`、`config_path`、`pending_transactions`、`total_transactions`、`backups`、`last_apply`、`last_restore`、`active_fields`。

### 2.3 `catalogue` — 已验证字段清单

```powershell
& $py -m hfk.cli catalogue
```

```text
已验证字段清单（每项都有源码证据）
==============================================================
[A] approvals.mode  —  审批主模式
      类型: manual / smart / off；默认: 'smart'
      作用: manual＝每个危险命令都问你；...
      证据: hermes_cli/config_defaults.py:1646-1680 ...
...
--------------------------------------------------------------
HFK 明确不修改（C 类）：
  · security.approval.transport：审批传输通道属于企业级安全边界...
  · auxiliary.approval.api_key：这是辅助审批模型的 API Key，属于秘密...
  · auxiliary.approval.provider：更换辅助审批模型提供商会改变...
代码级安全底线（不存在配置开关，因此无法被本工具削弱）：
  · HARDLINE_PATTERNS：... 
  · 凭据检测 / 凭据上传检测：...
```

这是判断「某个键到底能不能改」的唯一权威来源。清单之外的键（包括所谓 `fail_closed_when_no_human`）一律被拒绝。

### 2.4 `presets` — 四档预设分别改什么

```powershell
& $py -m hfk.cli presets
```

```text
默认预设：balanced
==============================================================
conservative  ——  保守
  几乎不改变现有行为：每个危险命令都询问，...
    · approvals.mode → manual
    · approvals.timeout → 300
    · approvals.cron_mode → deny
    · approvals.single_query_mode → deny
    · approvals.unattended_mode → deny
    · approvals.destructive_slash_confirm → True
    · approvals.mcp_reload_confirm → True
    · approvals.denial_breaker_threshold → 3
==============================================================
balanced  ——  平衡（默认）
  ...
  风险: 不改变任何「拦不拦」的判断，只改变「问几次、等多久」。
    · approvals.mode → smart
    · approvals.timeout → 120
    ...
==============================================================
freedom  ——  自由
  ...
  ⚠ 应用时需要 --acknowledge-risk
    · approvals.mode → off
    · approvals.timeout → 60
    · approvals.cron_mode → approve
    · approvals.single_query_mode → approve
    · approvals.unattended_mode → approve
    · approvals.destructive_slash_confirm → False
    · approvals.mcp_reload_confirm → False
    · approvals.denial_breaker_threshold → 0
==============================================================
custom  ——  自定义
  不使用任何预设，只应用你在下面逐项勾选的字段。
```

注意：上表是**预设定义**。真正写盘时，若某字段当前缺失且目标值等于 Hermes 默认值，会被跳过（见第 3 节）。

### 2.5 `preview` — 预览差异（只读）

```powershell
& $py -m hfk.cli preview --preset balanced
& $py -m hfk.cli preview --preset custom --set approvals.timeout=120
& $py -m hfk.cli --json preview --preset freedom
```

```text
补丁预览（预设 balanced）
配置文件: <HERMES_HOME>\config.yaml
当前哈希: 4a04e23542ca0a1f5dc627423d897f07a3b41301384d06197c1c2a476d5eb4ec
--------------------------------------------------------------
[略][A] approvals.mode
        审批主模式
        smart → smart
[改][A] approvals.timeout
        审批等待超时
        60 → 120
[略][A] approvals.cron_mode
        定时任务审批策略
        deny → deny
[略][A] approvals.single_query_mode
        单次查询审批策略
        （未设置） → deny   （当前未设置，目标值等于 Hermes 默认值，无需写入）
[略][A] approvals.unattended_mode
        无人值守平台审批策略
        （未设置） → deny   （当前未设置，目标值等于 Hermes 默认值，无需写入）
[略][A] approvals.destructive_slash_confirm
        破坏性斜杠命令二次确认
        false → false
[改][A] approvals.mcp_reload_confirm
        MCP 重载确认
        true → false
[改][A] approvals.denial_breaker_threshold
        连续拒绝熔断阈值
        （未设置） → 5
--------------------------------------------------------------
将写入 3 项；兼容性：支持
```

预览不写任何文件。退出码：无错误为 `0`；计划里存在错误（未知字段、非法取值、父级不是映射等）为 `1`，并在末尾打印 `✘` 行。

`--json` 的输出是 `PatchPlan` 加上逐项说明与兼容性：

```json
{
  "config_path": "<HERMES_HOME>\\config.yaml",
  "expected_sha256": "…",
  "preset": "balanced",
  "has_effect": true,
  "changes": [
    {
      "path": "approvals.timeout",
      "title": "审批等待超时",
      "before_exists": true,
      "before": 60,
      "after": 120,
      "changed": true,
      "risk": "A",
      "effect": "审批提示最多等多少秒；超时按 fail-closed 处理（视为拒绝）。",
      "error": "",
      "note": "",
      "display": "60 → 120"
    }
  ],
  "errors": [],
  "warnings": [],
  "explain": [ { "path": "approvals.timeout", "title": "…", "value": 120, "risk": "A", "effect": "…", "surfaces": ["CLI", "Gateway", "Desktop"], "evidence": "…" } ],
  "compat": { "supported": true, "exact": true, "version": "0.23.1", "family": "0.23.x", "reason": "精确匹配已验证版本 0.23.1", "manifest_version": "0.23.1" }
}
```

### 2.6 `apply` — 应用补丁

```powershell
& $py -m hfk.cli apply --preset balanced
& $py -m hfk.cli apply --preset custom --set approvals.timeout=120
& $py -m hfk.cli apply --preset freedom --acknowledge-risk
& $py -m hfk.cli apply --preset balanced --set 'approvals.deny=["git push --force*"]'
```

```text
✔ 补丁已应用
--------------------------------------------------------------
事务 ID   : tx-20261116T064113+0000-48361c7e
备份文件  : <HERMES_HOME>\hfk\backups\config-20261116T064113Z-9ec4b1.yaml
原始哈希  : 4a04e23542ca0a1f5dc627423d897f07a3b41301384d06197c1c2a476d5eb4ec
修改后哈希: e07ebb0d3aeca63ec67f8545b256410ccfc70e3accf35439b10d1d2275718c98
  · approvals.timeout: 60 → 120
  · approvals.mcp_reload_confirm: True → False
  · approvals.denial_breaker_threshold: None → 5
```

`apply` 会在真正写入前重新做一遍预览计算（不复用你上一次 `preview` 的计划），并强制校验：版本受支持、配置可读、计划无错误、至少有一项需要修改、文件 SHA-256 与本次计划一致。任何一步不满足都会拒绝写入（见第 8 节）。

`--json` 输出为事务记录：`transaction_id`、`before_sha256`、`after_sha256`、`backup_path`、`backup_sha256`、`preset`、`changes[]`（字段级 `before_exists`/`before`/`after`/`created_parents`）、`applied_at` 等，另附 `plan` 与 `config_sha256`。

常见拒绝：

| 报错 | 含义 |
|---|---|
| `预设「自由」需要显式确认风险后才能应用` | `freedom` 必须加 `--acknowledge-risk` |
| `没有需要修改的字段（全部已是目标值）` | 目标值已全部生效；`--preset custom` 没带 `--set` 也会这样 |
| `配置文件在预览之后发生了改变，拒绝写入` | 计划生成后文件被改动（`signature_mismatch`），重新预览再应用 |
| `未验证的 Hermes 版本 …` | 版本不在兼容清单内，只读 |
| `…不在已验证字段清单内，拒绝写入` | `--set` 用了白名单外的键 |

### 2.7 `restore` — 字段级恢复

```powershell
& $py -m hfk.cli restore                  # 按事务逆序恢复全部 pending 事务
& $py -m hfk.cli restore --id tx-...      # 只恢复指定事务
& $py -m hfk.cli --json restore
```

正常恢复：

```text
恢复结果：restored
--------------------------------------------------------------
  ✔ 已还原 approvals.denial_breaker_threshold
  ✔ 已还原 approvals.mcp_reload_confirm
  ✔ 已还原 approvals.timeout
```

存在冲突时：

```text
恢复结果：partially_restored
--------------------------------------------------------------
  ✔ 已还原 approvals.denial_breaker_threshold
  ✔ 已还原 approvals.mcp_reload_confirm
  ⚠ 冲突 approvals.timeout：当前值已被修改，保留你的新值，不覆盖
      当前值 180，HFK 曾写入 120，原始值 60
```

语义（务必理解）：

- 恢复是**字段级**的：只把 HFK 写过的键改回原值；如果原配置里没有这个键，就把 HFK 新建的键删掉，并清理 HFK 新建后变空的中间父级。
- 只有当字段**当前值仍等于 HFK 写下的值**时才还原；用户后来改过，就保留用户值并记为冲突。
- 不指定 `--id` 时，按事务**逆序**恢复所有 `applied` 状态的事务（后写的先撤）。`failed` / `restored` / `partially_restored` 的事务不会被再次恢复。
- 多条事务的恢复是“每条事务一次原子写”，不是整批一次原子操作；某条失败会保留它 pending 并继续提示。
- `restore` 同样要求版本受支持、配置可读；升级到未验证版本后无法用它回退。
- `--json` 键：`transaction_ids`、`restored`、`conflicts[]`（含 `path`/`current`/`hfk_wrote`/`original`/`reason`）、`skipped`、`warnings`、`status`。

### 2.8 `transactions` — 事务历史

```powershell
& $py -m hfk.cli transactions
```

```text
事务共 1 条
--------------------------------------------------------------
tx-20261116T064113+0000-48361c7e  [applied]  2026-11-16T06:41:13+00:00
  预设 balanced，Hermes 0.23.1，Profile default
  备份 <HERMES_HOME>\hfk\backups\config-20261116T064113Z-9ec4b1.yaml
    · approvals.timeout: 60 → 120 (applied)
    · approvals.mcp_reload_confirm: True → False (applied)
    · approvals.denial_breaker_threshold: None → 5 (applied)
```

状态取值：`applied`（未恢复）、`restored`、`partially_restored`、`failed`（写入失败并已回滚，保留备份）。

### 2.9 `backups` — 备份列表

```powershell
& $py -m hfk.cli backups
```

```text
备份目录：<HERMES_HOME>\hfk\backups
共 1 个备份
  · config-20261116T064113Z-9ec4b1.yaml  (18800 字节)
```

备份是 `config.yaml` 的**字节级原始副本**，文件名 `config-<UTC 时间戳>-<随机 hex>.yaml`。每次 `apply` 都会新建一个；失败的事务也保留自己的备份；`restore` 和 `uninstall` **都不删除**备份。

### 2.10 `uninstall` — 卸载 HFK

```powershell
& $py -m hfk.cli uninstall
& $py -m hfk.cli uninstall --keep-plugin
```

```text
卸载完成
--------------------------------------------------------------
恢复状态  : restored
已删插件  : 是
保留备份  : 1 个
卸载记录  : <HERMES_HOME>\hfk\uninstalled.json
  · Hermes 本体、用户配置、SOUL.md 与 Skills 均未被修改
```

卸载顺序与保证：

1. 先执行 `restore`（全部 pending 事务，字段级、带冲突检测）。
2. 若 `restore` 抛错（例如版本不受支持、配置不可读），卸载会**中止**，不会删任何东西。
3. 删除 `<HERMES_HOME>\desktop-plugins\hfk\`（除非 `--keep-plugin`），删除前校验路径必须位于 Home 内。
4. 写入 `<HERMES_HOME>\hfk\uninstalled.json`（时间、版本、恢复报告、保留的备份数）。
5. **保留**：`hfk/backups/`、`hfk/transactions/transactions.json`、`hfk/logs/`、以及配置本身。

`uninstall` **不删除** `hfk/` 目录本身；如果服务异常退出，`hfk/service.json` 也可能残留（正常 Ctrl+C 退出会删除它）。

### 2.11 `service` — 本地 core 服务

```powershell
& $py -m hfk.cli service
& $py -m hfk.cli service --port 8765 --print-token
```

```text
HFK Core 服务已启动：http://127.0.0.1:8765
令牌发现文件：<HERMES_HOME>\hfk\service.json（权限 0600）
HFK_TOKEN=<只在你加 --print-token 时打印>
按 Ctrl+C 停止。仅监听 127.0.0.1，不暴露公网端口。
```

- 每次启动生成新的随机令牌（`secrets.token_urlsafe(32)`），写入 `service.json`（`chmod 0600`），供本机 Desktop 插件读取；令牌不会写进日志，也不会在 HTTP 响应里回显。
- 仅绑定 `127.0.0.1`；`Host` 头必须是回环，否则 `403 bad_host`。
- 请求头 `X-HFK-Token` 错误或缺失 → `401 unauthorized`（`/api/health` 例外，它无需令牌）。
- 没有任何命令执行端点：路由是固定白名单。

HTTP 接口（服务层固定动作集合）：

| 方法 | 路由 | 作用 |
|---|---|---|
| GET | `/api/health` | 存活探针，返回 `{"ok": true, "hfk_version": …}`（**无需令牌**） |
| GET | `/api/env` `/api/status` `/api/catalogue` `/api/presets` `/api/transactions` `/api/backups` `/api/config` | 只读视图 |
| POST | `/api/preview` `/api/apply` `/api/restore` `/api/uninstall` | 与 CLI 同名操作 |

POST 请求体支持 `preset`、`overrides`、`items`、`acknowledge_risk`（apply）、`transaction_id`（restore）、`remove_plugin`（uninstall）；最大 1 MiB。服务的 Home 总是自动探测（不接受 CLI 的 `--home`），多 Home 请用 `HERMES_HOME` 环境变量。

### 2.12 `selftest` — 内置自检

```powershell
& $py -m hfk.cli selftest
```

```text
场景 1｜YAML 语义
  ✔ 写入后仍为 CRLF、无裸 LF
  ✔ 注释全部保留
  ✔ mode: off 被加引号（YAML 1.1 安全）
  ...
场景 2｜Apply → Restore 字节级保真
  ...
--------------------------------------------------------------
自检通过：29 项检查全部成功
```

6 个场景：YAML 语义、Apply/Restore 字节保真、冲突检测、多次 Apply 逆序恢复、未知版本禁止写入、卸载保留备份与用户配置。全部在系统临时目录里的沙箱执行，结束即删除，**不会触碰真实配置**。

## 3. 预设 vs 逐项选择

### 3.1 组合规则

最终写什么 = **预设字段集合** + **`--set` 覆盖**：

```text
最终字段 = preset_items(预设) 然后逐项被 --set 覆盖
```

- 不写 `--preset` 时默认用 `balanced`。所以 `--set approvals.timeout=120` 单独使用 = “balanced + 覆盖 timeout”，**不是**“只改 timeout”。
- 只想精确改某几项：用 `--preset custom`（它本身不带任何字段）：

```powershell
& $py -m hfk.cli apply --preset custom --set approvals.timeout=180
```

- `--set` 可以重复；list 类型可用 JSON 或逗号分隔：

```powershell
--set 'approvals.deny=["git push --force*"]'
--set 'approvals.approvers=alice,bob'
```

- 取值解析按字段类型进行，**不做 YAML 标量推断**：因此 `approvals.mode=off` 的 `off` 会按字符串写入，并被加引号（YAML 1.1 下裸 `off` 会被读成布尔 `false`）。
- CLI 的 `--set` 只能覆盖/新增，不能从预设里“剔除”某一项（这是内部 API 的能力，未暴露到命令行）。

### 3.2 预设与字段对照

| 字段 | conservative | balanced | freedom | custom |
|---|---|---|---|---|
| `approvals.mode` | `manual` | `smart` | `off` | — |
| `approvals.timeout` | `300` | `120` | `60` | — |
| `approvals.cron_mode` | `deny` | `deny` | `approve` | — |
| `approvals.single_query_mode` | `deny` | `deny` | `approve` | — |
| `approvals.unattended_mode` | `deny` | `deny` | `approve` | — |
| `approvals.destructive_slash_confirm` | `true` | `false` | `false` | — |
| `approvals.mcp_reload_confirm` | `true` | `false` | `false` | — |
| `approvals.denial_breaker_threshold` | `3` | `5` | `0` | — |

其余字段（`discord_prompt_timeout`、`memory.write_approval`、`skills.write_approval` 以及全部 B 类）**不属于任何预设**，只能逐项 `--set`。

### 3.3 “默认值不落盘”规则

如果某字段**当前不存在**，而目标值恰好等于 Hermes 默认值，HFK 不会写它——因为写了也不改变行为，只会让 `config.yaml` 变脏。预览里会显示：

```text
[略][A] approvals.single_query_mode
        单次查询审批策略
        （未设置） → deny   （当前未设置，目标值等于 Hermes 默认值，无需写入）
```

该规则只针对 enum/int/bool/str 这类标量（`schema.FieldSpec.kind`），列表字段不套用。

### 3.4 风险门控（重要）

- `freedom` 预设必须 `--acknowledge-risk` 才能应用。
- **B 类字段不在任何预设里，但 `--set` 可以直接写**（这是“逐项显式选择”的通道）。`--acknowledge-risk` **不**门控 B 类字段；B 类的提示只出现在 `preview` 的计划警告里：

```text
[改][B] approvals.smart_policy
        智能审批附加策略
        （未设置） → 涉及 /etc 一律升级
...
⚠ 智能审批附加策略（approvals.smart_policy）属于高风险项：追加到智能审批「守卫模型」系统提示末尾的额外规则…写反了会削弱守卫本身的判断。
```

`apply` 的文本输出不重复打印这些警告（`--json` 里的 `plan.warnings` 可见）。**因此写 B 类字段前请先 `preview`**。

## 4. 如何阅读一个 preview diff

| 元素 | 含义 |
|---|---|
| `[改]` / `[略]` | 是否真的会写入（`changed` 为真才写） |
| `[A]` / `[B]` / `[C]` | 风险级别；C 只会出现在错误项（白名单外字段） |
| `当前值 → 目标值` | `before → after`；`（未设置）` 表示当前配置里没有这个键 |
| 行尾 `（当前未设置，目标值等于 Hermes 默认值，无需写入）` | 被“默认值不落盘”规则跳过 |
| `✘` 行 | 该字段有错误（未知字段 / 非法取值 / 父级不是映射），`apply` 会被整体拒绝 |
| `将写入 N 项；兼容性：支持` | N 是真正会写盘的字段数（不是预设字段总数） |
| `⚠` 行 | 计划警告（B 类字段、或所有字段都已是目标值） |

只看 JSON 时：用 `changes[].changed` 判断是否写盘，`changes[].error` 与顶层 `errors` 判断是否会被拒绝，`expected_sha256` 是本次计划绑定的文件哈希。

## 5. 冲突：长什么样、怎么办

**冲突的定义**：执行 `restore` 时，某字段的**当前值不等于 HFK 当初写下的值**。这意味着 `apply` 之后有人（你、另一个工具、或 Hermes 自己）改过它，HFK 拒绝覆盖你的修改。

| `reason` | 场景 | HFK 的行为 |
|---|---|---|
| `当前值已被修改，保留你的新值，不覆盖` | 字段存在且值 ≠ HFK 写入值 | 不改动该字段；事务标记 `partially_restored` |
| `字段已被删除（可能是你手动删的），保留删除状态` | 字段现在不存在，但 HFK 当初写过非空值 | 不重建该字段 |

冲突时的处理：

1. **默认什么都不做**：这是安全行为，你的值被保留。
2. 想让字段回到 HFK 写入前的值：先确认你知道原值（`restore --json` 的 `original` 字段），再手工改回；或者重新 `apply` 覆盖。
3. 如果想彻底撤销 HFK 的影响：先处理冲突字段，再执行一次 `restore`（冲突事务不会重复恢复，但你可以对新 Apply 的事务再恢复）。

`restore --json` 的冲突结构：

```json
{
  "transaction_ids": ["tx-..."],
  "restored": ["approvals.mcp_reload_confirm"],
  "conflicts": [
    {
      "path": "approvals.timeout",
      "current_exists": true,
      "current": 180,
      "hfk_wrote": 120,
      "original": 60,
      "original_existed": true,
      "reason": "当前值已被修改，保留你的新值，不覆盖"
    }
  ],
  "skipped": [],
  "warnings": [],
  "status": "partially_restored"
}
```

## 6. 卸载保证

`uninstall` 的明确承诺：

| 会做 | 不会做 |
|---|---|
| 先字段级 `restore` 全部 pending 事务 | 不删除 Hermes 本体 / 安装目录 |
| 删除 `<HERMES_HOME>\desktop-plugins\hfk\`（可 `--keep-plugin` 保留） | 不删除或重写 `config.yaml` 之外的任何 Hermes 文件 |
| 写 `hfk\uninstalled.json` 卸载记录 | 不删除 `hfk\backups\`、`hfk\transactions\`（留作审计/手工回退） |
| 统计并保留备份数量 | 不删除 SOUL.md、Skills、记忆、会话数据库 |
| 保持配置可被 Hermes 正常读取 | 不触碰网关进程 |

注意：因为第 1 步依赖 `restore`，**在不受支持的 Hermes 版本上 `uninstall` 会直接报错中止**；此时请先手工回退（见第 8 节），或用兼容的 HFK 版本处理。

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `UnicodeEncodeError: 'gbk' codec can't encode character '\u2714'` | GBK 代码页控制台无法输出 `✔/✘/⚠` | `$env:PYTHONUTF8='1'`（或 `chcp 65001`，或改用 `--json`） |
| `hfk: error: unrecognized arguments: --json` | 全局参数放在了子命令后面 | 改为 `hfk --json status` |
| `environment_not_found` / 提示找不到 Hermes Home | 自动探测失败 | 用 `--home` 指定，或设置 `HERMES_HOME`；确认目录里有 `config.yaml`/`.env`/`state.db` |
| 报 `config_locked` 或写入时遇到 `WinError 32/5/33` | 网关/编辑器短暂持有 `config.yaml`；Windows 上 `os.replace` 争用 | HFK 会自动重试 6 次（0.02s 起、×1.7 退避），仍失败则退回原地重写；反复失败时先停止占用方（如 Hermes 网关）再重试 |
| `unsupported_version`：`未验证的 Hermes 版本 …` | 版本不在 `compat/manifests` 里 | 只读模式；换回 0.23.1，或在同族 `0.23.x` 上重新预览确认后再应用（能否写入以 `status.supported` 为准） |
| `config_read_error`：配置无法解析 / 顶层不是映射 | `config.yaml` 存在 YAML 语法问题或结构异常 | 先手工修复 YAML（HFK 绝不写坏文件）；修复后 `env` 应显示“配置可读: 是” |
| `signature_mismatch`：配置文件在预览之后发生了改变 | 计划与写入之间文件被改 | 重新 `preview` 再 `apply`；不要复用旧计划 |
| `backup_error`：备份校验未通过，拒绝继续覆盖 | 新建的备份与原文件哈希不一致（磁盘/权限问题） | Apply 已中止、未写入；检查磁盘空间/权限后重试 |
| `restore` 报“找不到可恢复的事务” | 该事务不是 `applied`（已恢复/失败），或 ID 不存在 | `transactions` 查看真实状态；只恢复 applied 事务 |
| `restore` 提示某事务“备份已缺失，不会被恢复，也不会被删除” | 有人删了 `hfk/backups/` 里的文件 | 该事务保持 pending；从别处/手工回退，或接受现状。注意：恢复本身依据事务里的字段级 before/after，**不读取**备份字节 |
| `restore` 返回 `restored` 但 `restored` 为空 | 没有待恢复事务 | 看 `warnings`（会写“没有待恢复的 HFK 事务”）；无需处理 |
| `apply` 报“没有需要修改的字段” | 目标值已全部生效；或 `--preset custom` 没带 `--set` | 用 `preview` 确认，或加 `--set` |
| `apply` 报“父级 … 已存在但不是映射” | 例如 `approvals: broken`，写入会丢失原值 | HFK 拒绝写入；先手工把父级改成映射（或删掉错误值） |
| `freedom` 被拒 | 缺 `--acknowledge-risk` | 加参数；先读懂 `presets` 的风险说明 |
| 插件显示离线 / 调不通服务 | core service 没启动，或令牌变了 | 启动 `hfk service`；插件读 `hfk\service.json` 获取端口与令牌；每次重启令牌都会变 |
| HTTP `401 unauthorized` | 缺 `X-HFK-Token` 或令牌过期（服务重启后旧令牌失效） | 重新从 `service.json` 读取，或用 `--print-token` 注入 |
| HTTP `403 bad_host` | `Host` 不是回环地址（DNS rebinding 防护） | 用 `http://127.0.0.1:<port>` 访问 |
| 服务里 `POST /api/apply` 返回 400 | 业务拒绝（版本/校验/无变化） | 响应体 `error.code` 可区分，见本表其余行 |
| 同族 `0.23.x` 上 `compat.exact=false` | 字段未逐版本复核 | 必须重新预览；不确定就不要 apply |

## 8. 安全回退（step-by-step）

目标：把 `config.yaml` 恢复到 HFK 修改之前，同时不丢失 HFK 之后的任何合法修改。

**第 0 步：不要手工整体另存/覆盖。** HFK 的事务里已经记录了每个字段的 `before`，逐字段回退比整文件替换安全。

**第 1 步：确认当前状态**

```powershell
$env:PYTHONUTF8 = "1"
& $py -m hfk.cli status
& $py -m hfk.cli transactions
```

看到 `未完成事务: N`，记下每个 `tx-...` 的 ID 与它改过的字段。

**第 2 步：单条回退（推荐先小范围验证）**

```powershell
& $py -m hfk.cli restore --id tx-20261116T064113+0000-48361c7e
```

**第 3 步：全部回退（按事务逆序）**

```powershell
& $py -m hfk.cli restore
```

**第 4 步：核对结果**

- 输出 `恢复结果：restored` 且每个字段都有 `✔ 已还原` → 完成。
- 输出 `partially_restored` 且有 `⚠ 冲突` → 这些字段是你后来改过的，HFK 已保留你的值；决定是保留还是手工改回。
- 再跑一次 `status`：`未完成事务` 应为 `0`。

**第 5 步：验证配置仍可被 Hermes 读取**

```powershell
& $py -m hfk.cli env
```

`配置可读: 是` 即 YAML 结构正常（HFK 的写入本身带回读校验，这一步是额外确认）。

**第 6 步（仅在 HFK 数据损坏或想整体回到某个时间点）：使用字节级备份**

`hfk\backups\config-*.yaml` 是当时的**原始文件副本**。操作要点：

1. 关闭正在编辑/占用 `config.yaml` 的程序（避免边写边覆盖）。
2. 把选定的备份**复制**（不是移动）到别处，改名为 `config.yaml` 后再覆盖目标；覆盖前再留一份当前文件。
3. 这会把备份之后的所有配置修改一并丢弃，只作为最后手段。

**第 7 步：确认无误后再卸载**

```powershell
& $py -m hfk.cli uninstall
```

它会再执行一次 `restore`（幂等：没有可恢复事务就只给警告），然后清理插件目录并写卸载记录；备份与事务历史保留。

**回退失败时的兜底**：如果 `restore` 因版本不受支持被拒绝，直接走第 6 步（备份是字节原件，不依赖 HFK 引擎）；如果连 `hfk/backups/` 都没了，按 `status.active_fields` 与 `transactions` 打印的 `before` 值手工逐项改回。
