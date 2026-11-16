# HFK 架构说明

本文描述 HFK 0.1.0 的内部结构：每个模块负责什么、一次 Apply 从计划到落盘到底经过哪些步骤、事务与备份如何建模、兼容性如何注册、本地服务与 Desktop 插件如何分层，以及新增一个 Hermes 版本需要做什么。

## 1. 设计约束

1. **不导入 Hermes**：`hfk/**` 不 import 任何 `hermes_cli` / `tools` / `utils` 模块。Hermes 升级、Desktop 换架构都不会直接打断 HFK 的引擎（唯一例外是开发期探针脚本，见 4.4）。
2. **只做字段级写入**：一次加载、一次原子替换；绝不用整份配置覆盖用户文件。
3. **先把安全做成数据**：能改哪些键、每个键的类型/取值/风险级别，全部集中在 `hfk/schema.py`，代码里没有第二份白名单。
4. **失败也是一等公民**：写入失败必须回滚到原始字节，并留下 `failed` 事务和备份；绝不把“没做完”记成“做完了”。
5. **写出的字节要与 Hermes 自己写的一致**：否则每次补丁都会产生整文件 diff，违反“字段级修改”的承诺。

## 2. 模块地图

### 2.1 核心层

| 文件 | 职责 | 关键点 |
|---|---|---|
| `hfk/__init__.py` | 包标识 | 只导出 `HFK_NAME` / `HFK_VERSION`；显式声明不导入 Hermes |
| `hfk/version.py` | 版本常量 | `HFK_VERSION = "0.1.0"`；`TRANSACTION_SCHEMA_VERSION = 1`（与 HFK 版本解耦） |
| `hfk/errors.py` | 异常体系 | 基类 `HFKError` 带 machine-readable `code`；`SignatureError` 继承 `ConflictError`（并发修改） |
| `hfk/util.py` | 通用工具 | `sha256_file`、`utc_now_iso`、`json_dump_atomic`（临时文件 + fsync + replace）、`assert_within` 路径越界守卫、`to_plain`（把 ruamel 节点转成可 JSON 化的普通对象） |
| `hfk/env.py` | 宿主探测 | 定位 Home / 安装目录；版本三源（install-stamp → git tag → gateway_state）；Gateway 存活（Windows 用 `OpenProcess` 而非 `os.kill`）；配置可读性；未完成事务数 |
| `hfk/schema.py` | **安全核心** | `CATALOGUE`（17 个已验证字段）、`BLOCKED_FIELDS`（C 类显式排除）、`CODE_LEVEL_FLOORS`（代码级底线说明）、`validate_value`；A/B/C 风险分级 |
| `hfk/config/keypath.py` | 点分键路径 | 转义点、最长字面点键优先（`grok-4.6` 整体成键）、`get_nested` 与写入导航一致 |
| `hfk/config/yaml_io.py` | YAML 读写 | 复刻 Hermes 的 round-trip 发射器策略；`update_keys` 一次加载/一次原子替换；`atomic_write_bytes` 用于字节级回滚 |
| `hfk/profiles/presets.py` | 预设 | 四档预设只是字段取值别名；`compose` 组合预设 + 逐项覆盖；`explain` / `diff_against` 供 UI |
| `hfk/compat/__init__.py` | 兼容判定 | 精确匹配 manifest → 同族降级 → 未知版本只读；`CompatResult(supported, exact, family, manifest)` |
| `hfk/compat/manifests/hermes-0.23.1.json` | 0.23.1 注册清单 | 版本身份、验证依据、`supported_fields`（与 `CATALOGUE` 一致，测试强制）、已知问题 |

### 2.2 引擎层

| 文件 | 职责 | 关键点 |
|---|---|---|
| `hfk/engine/backup.py` | 字节级备份 | `create_backup` 用 `open(..., "xb")` 排他创建、fsync、回读哈希与源文件比对；`verify_backup` 每次使用前重算哈希 |
| `hfk/engine/transaction.py` | 事务存储 | `Transaction` / `Change` 数据类；`TransactionStore` 原子读写 `transactions.json`；`is_pending` 只认 `applied` |
| `hfk/engine/patch.py` | 引擎主体 | `build_plan`：校验 + 差异 + 默认值跳过 + SHA 绑定；`apply`：备份 → 写入 → 回读 → 事务；`restore`：字段级、冲突检测、逆序；`uninstall`；`status` |
| `hfk/facade.py` | 高层门面 | CLI / HTTP 服务共用；负责刷新环境、preset 风险门控、把引擎结果整理成可序列化视图 |

### 2.3 接口层

| 文件 | 职责 | 关键点 |
|---|---|---|
| `hfk/cli.py` | 命令行 | `argparse`；`--home/--install-dir/--json` 是**全局**参数（必须在子命令前）；`--set` 按字段类型解析，不做 YAML 标量推断 |
| `hfk/service/app.py` | 本地 HTTP 服务 | 仅 `127.0.0.1`；随机令牌；`X-HFK-Token`；`Host` 回环校验；固定路由集合，无命令执行端点；`service.json`（0600）做令牌发现 |
| `hfk/testing.py` | 测试沙箱 | 合成 `<root>/home/config.yaml` + `<root>/install/hermes_cli` + `install-stamp.json`，不改真实环境 |
| `hfk/selftest.py` | 内置自检 | 6 个场景，全部在临时目录执行，结束删除 |

### 2.4 开发期脚本与测试

| 文件 | 职责 | 说明 |
|---|---|---|
| `scripts/probe_schema.py` | 从 Hermes `DEFAULT_CONFIG` 导出审批相关路径 | 需要 Hermes venv 且 cwd = Hermes 仓库根；用于构建/复核字段清单 |
| `scripts/parity_probe.py` | 与 Hermes 写入器做字节对照 | 调 `utils.atomic_roundtrip_yaml_update`，比较产物字节、改动行数、CRLF、`off` 引号语义；路径写死，属于开发期探针而非单元测试 |
| `tests/test_hfk.py` | 引擎/服务端到端 | 键路径、YAML 保真、事务、回滚、并发签名、卸载、HTTP 令牌 |
| `tests/test_schema.py` | 清单一致性 | 字段表 ↔ manifest ↔ 预设三方一致；默认值/枚举/风险等级回归 |
| `pyproject.toml` | 打包 | `requires-python >= 3.11`；依赖 `ruamel.yaml>=0.18.16,<0.20`；entry point `hfk = hfk.cli:main`；清单 JSON 作为 package data |

## 3. 写入路径端到端

### 3.1 探测（只读）

`HFK.connect()` → `env.detect()`。要点：

- Home 解析顺序：显式 → `HERMES_HOME` → `%LOCALAPPDATA%\hermes`（含 `HERMES_DATA_DIR_SUFFIX`）→ `~/.hermes`；候选目录需含 `config.yaml`/`.env`/`state.db`。
- 安装目录解析：gateway 命令行里的 `.py` 父父目录 → `HERMES_AGENT_DIR` → `<home>\hermes-agent` → `%LOCALAPPDATA%\hermes\hermes-agent`；要求存在 `hermes_cli/`。
- 版本：`install-stamp.json` 的 `displayVersion`/`baseVersion` → `git describe --tags` → `gateway_state.json.code_version`。三源不一致、git dirty、Desktop `package.json` 为 `0.0.0` 都只进入 `warnings`，不抛异常。
- 配置可读性：用 `hfk.config.yaml_io.read_text` + YAML 1.1 safe 解析；顶层必须是映射。读不出就记录 `config_error`，后续写入阶段会拒绝。
- 探测失败不会中断应用流程的“查看”部分；**是否允许写入由 `_assert_writable` 二次判定**。

### 3.2 计划：`build_plan`

`hfk/engine/patch.py::build_plan(env, items, preset=...)`：

1. `_load_plain(env)`：YAML 1.1 安全读取当前配置；文件缺失/解析失败/顶层非映射都抛 `ConfigReadError`（不写）。
2. 对每个目标字段：
   - `get_spec` 找字段定义（找不到 → 风险标记为 C，`validate_value` 会给出“不在已验证字段清单内”）。
   - `validate_value`：类型/枚举/范围校验，返回错误文案列表。
   - `_refuse_scalar_parents`：若父级已存在但不是映射（如 `approvals: broken`），**拒绝**而不像整文档写入器那样静默替换掉原值。
   - `get_nested` 取当前值；`_same` 做**严格**相等（`True == 1` 在 Python 为真，但 YAML 语义不同，所以单独排除 bool/int 混用）。
   - “默认值不落盘”：字段**当前缺失**、目标值等于 `FieldSpec.default`、且 kind 为 enum/int/bool/str 时，标记 `changed=False` 并附说明（列表不适用）。
3. 汇总 `errors`、`warnings`；B 类字段只要有实际变化就追加显式警告。
4. 记录 `expected_sha256 = sha256_file(config_path)`——这是后续 CAS 的基准。

产物 `PatchPlan` 含 `changes[]`（每项 `before_exists/before/after/changed/risk/effect/error/note`）、`errors`、`warnings`、`writable`（真正要写的字段）。

### 3.3 应用：`apply`

`facade.apply()` 先 `refresh()`（重新探测，避免用旧快照），然后：

```text
facade.apply
  ├─ preset 风险门控（freedom 需要 acknowledge_risk）
  ├─ patch.build_plan（重新计算，不复用上一次 preview）
  └─ patch.apply(env, plan)
       ├─ _assert_writable：compat.supported 且 config_readable
       ├─ plan.errors 非空 → ConfigValidationError
       ├─ plan.writable 为空 → ConfigValidationError（没有需要修改的字段）
       ├─ current_sha != plan.expected_sha256 → SignatureError（CAS 失败）
       ├─ 重新加载文档；逐路径 _refuse_scalar_parents；计算 created_parents
       ├─ create_backup（原始字节 → backups/config-*.yaml，xb 排他创建 + fsync）
       ├─ verify_backup + read_backup_bytes（哈希不符则拒绝继续）
       ├─ update_keys(writes)：一次 ruamel round-trip 加载 → 改字段 → 临时文件 → fsync → os.replace
       ├─ _verify_written：回读每个字段，必须严格等于目标值
       │    └─ 失败 → atomic_write_bytes(原始字节) 回滚 + 记 failed 事务 + 抛 ConfigWriteError
       ├─ after_sha256 = sha256_file(...)
       └─ Transaction(status=applied, changes=[...]) → TransactionStore.append
```

细节说明：

- **CAS（compare-and-swap）**：`preview` 时把文件 SHA-256 写进计划；`apply` 写入前重新计算并比对。因此“先 preview、后 apply”能发现中间的文件改动；`facade.apply` 内部重新 `build_plan`，所以即使你不 preview 直接 apply，也会在本次计划与写入之间做同样的比对。
- **单次写入**：`update_keys` 在一次加载/一次落盘中修改所有目标字段，不存在“改了一半”的中间状态。删除键的语义是“从映射中 `pop`”，不是写 `null`。
- **created_parents**：写入前记录“会被新建的中间父级路径”，随事务持久化；恢复时若这些节点变空则清理（原本存在的节点绝不动）。
- **写后校验**：不是只看写入函数没抛异常，而是重新解析文件、逐字段严格比对值；不符就回滚。
- **回滚**：用**备份的原始字节**原子写回（`atomic_write_bytes`），不重新序列化，因此能保证字节级还原。

### 3.4 失败路径

| 阶段 | 失败处理 |
|---|---|
| 计划阶段 | 不写任何文件；`preview` 以退出码 1 结束，`apply` 抛 `ConfigValidationError` |
| CAS 失败 | 不写；抛 `SignatureError`（`conflict_error` / `signature_mismatch`） |
| 备份创建/校验失败 | 不覆盖配置；抛 `BackupError` |
| 写入异常 | 用备份字节回滚 + 记 `failed` 事务（含备份路径）+ 抛 `ConfigWriteError` |
| 写后校验失败 | 同上（回滚 + `failed` 事务 + `ConfigWriteError`） |
| 事务记录写入失败 | 配置已写入，异常向上抛出；**不会**自动回滚配置（备份仍在；见安全模型的残余风险） |

### 3.5 恢复：`restore`

1. `TransactionStore.load()`；`pending = [tx for tx in transactions if tx.is_pending]`（只有 `applied`）。
2. `--id` 指定时必须命中 pending，否则 `ConfigValidationError`；不指定则 `reversed(pending)`（后写的先撤）。
3. `_assert_writable`（版本受支持、配置可读）。
4. 对每条事务：
   - 备份文件不存在 → 追加警告，**不恢复也不删除**该事务（保持 pending）。
   - 重新加载文档；按 `reversed(transaction.changes)` 逐字段判断：
     - 当前值 `==` HFK 写入值 → 加入 `updates`：原来有值则写回 `before`，原来没有则 `None`（删除）。
     - 字段现在不存在 → 若 `after is None` 记 `skipped`；否则记冲突“字段已被删除，保留删除状态”。
     - 字段存在但值不同 → 记冲突“当前值已被修改，保留你的新值，不覆盖”。
   - `update_keys(updates)`：失败则追加“事务保持 pending，可重试”并跳过该事务。
   - `_cleanup_created_parents`：删除 HFK 新建且现已为空的父级映射（深的先删）。
   - 事务状态改为 `partially_restored`（有冲突）或 `restored`；写回 `restored_at`。
5. 报告 `status`：有冲突 → `partially_restored`；有恢复项 → `restored`；否则 → `applied`。

注意：多事务恢复是“每条事务一次原子写”，不是整批一个事务；中途失败时前面的事务已标记恢复、后面的仍是 pending。

### 3.6 卸载：`uninstall`

`restore()` → 删除 `<home>/desktop-plugins/hfk`（`assert_within(home)` 守卫；`--keep-plugin` 可保留）→ 统计保留备份数 → 写 `hfk/uninstalled.json`。`restore()` 抛错时整个卸载中止，不删任何东西。

## 4. 为什么独立实现、却复刻 Hermes 的 YAML 策略

### 4.1 独立的理由

Hermes 的内部模块（`hermes_cli.config`、`utils`、`hermes_yaml`）不是稳定 API；HFK 作为宿主补丁工具，不能因为上游重构而失效。因此核心引擎只依赖 `ruamel.yaml` + 标准库。

### 4.2 复刻发射器策略（逐项对照）

HFK 的 `hfk/config/yaml_io.py` 与 Hermes 0.23.1 的 `hermes_yaml.py` 逐参数一致：

| 策略 | HFK | Hermes 0.23.1 |
|---|---|---|
| round-trip 模式 | `YAML(typ="rt")` | `YAML(typ="rt")` |
| 禁止折行 | `width = 2**31 - 1` | `ROUNDTRIP_YAML_WIDTH = 2**31 - 1`（原因：ruamel 折行会把 `D:\` 后的内容变成空格，值被改动） |
| YAML 版本 | 自定义 `VersionedResolver.processing_version = (1, 1)` | 同名同实现的 `_Yaml11Resolver` |
| 引号 | `preserve_quotes = True`；歧义字符串强制双引号 | 同 |
| 缩进 | `indent(mapping=2, sequence=4, offset=2)` | 同 |
| Unicode | `allow_unicode = True` | 同 |
| 流程风格 | `default_flow_style = False` | 同 |
| 只读校验 | `YAML(typ="safe", pure=True)` + `version=(1, 1)` | `_load(pure=True)`（先试 C 解析器，失败回退 pure） |

HFK 额外做的三件事：

1. **读入换行归一化**：Hermes 用 `Path.read_text()`（universal newlines），若把 CRLF 原样喂给 ruamel，`\r` 会进入注释 token，回写时再被平台换行翻译，导致“每条注释后多一个空行”（开发期实测：714 行的配置会膨胀到 735 行）；HFK 读入时统一成 `\n`，写出时用 `newline=None`（Windows 上即 CRLF）。
2. **原子替换加强**：同目录 `mkstemp` → 写 → `fsync` → `os.replace`；遇到 Windows 的 `WinError 5/32/33`（句柄占用）做 6 次退避重试，仍失败退回原地重写，避免补丁完全写不进去。
3. **符号链接写穿**：目标是 symlink 时先 `realpath` 再替换，保证写的是目标文件而不是把链接换掉。

### 4.3 为什么复刻而不是 import

- 若直接 import Hermes 的 `yaml` 实例/函数，HFK 就被绑死在它的模块布局上；
- 但写出的 `config.yaml` 又必须与 Hermes 自己写的一致，否则每次补丁都是整文件 diff；
- 结论：**策略复刻 + 字节对照验证**。这是“故意重复”的一处设计，注释里写明了原因，避免后来者误以为是没必要的复制粘贴。

### 4.4 字节一致性怎么证明

`scripts/parity_probe.py`（开发期探针）：

1. 复制真实 `config.yaml` 两份；
2. 一份用 HFK 的 `update_key` 改 `approvals.timeout`，另一份用 Hermes 的 `utils.atomic_roundtrip_yaml_update` 改同一字段；
3. 断言两份产物**字节完全一致**、一次字段修改只改动 **1 行**、CRLF 保持、`approvals.mode: off` 被加引号且 YAML 1.1 读回是字符串 `off`。

注意：探针的源文件路径、Hermes 仓库路径都是写死的，且需要 Hermes 的 `utils` 可导入（cwd = Hermes 仓库根、Hermes venv 解释器），所以它只是开发期验证工具，不在 `unittest` 里；单元测试覆盖的是 HFK 侧的等价不变量（CRLF、注释保留、`off` 引号、单字段改动后文件其余字节不变）。

## 5. 键路径语义

`hfk/config/keypath.py` 与 Hermes `hermes_cli/config` 的导航规则一致：

1. `.` 是层级分隔符，`\.` 表示键名里的字面点；
2. 在已有映射里导航时，优先匹配**最长字面点键**（模型 ID 如 `grok-4.6`、`glm-5.3` 整体作为一个键，而不是拆成 `grok-4: {6: ...}` 的幻影节点）；
3. `get_nested` 与写入共用同一套导航逻辑（`patch._chain` 也是），所以“检查的”就是“会写的”；
4. `phantom_sibling` 提供“新建中间映射会遮蔽既有字面键”的检测函数（当前写路径未接线，仅作为工具函数保留）。

## 6. 事务与备份数据模型

### 6.1 数据目录

```text
<HERMES_HOME>/hfk/
├─ backups/
│  └─ config-<UTC 时间戳>-<随机 hex>.yaml   # config.yaml 的字节级原件
├─ transactions/
│  └─ transactions.json                     # 全部事务（原子写）
├─ logs/
│  └─ hfk.log                               # 服务日志（不含令牌/秘密）
├─ service.json                             # 服务发现：pid/port/base_url/token/…（0600）
└─ uninstalled.json                         # 卸载记录（uninstall 时写）
```

### 6.2 `transactions.json`

```json
{
  "schema_version": 1,
  "updated_at": "2026-11-16T06:41:13+00:00",
  "transactions": [
    {
      "schema_version": 1,
      "transaction_id": "tx-20261116T064113+0000-48361c7e",
      "hfk_version": "0.1.0",
      "hermes_version": "0.23.1",
      "profile": "default",
      "timestamp": "2026-11-16T06:41:13+00:00",
      "status": "applied",
      "preset": "balanced",
      "config_path": "<HERMES_HOME>\\config.yaml",
      "before_sha256": "…",
      "after_sha256": "…",
      "backup_path": "<HERMES_HOME>\\hfk\\backups\\config-….yaml",
      "backup_sha256": "…",
      "changes": [
        {
          "path": "approvals.timeout",
          "before_exists": true,
          "before": 60,
          "after": 120,
          "status": "applied",
          "created_parents": [],
          "note": ""
        }
      ],
      "applied_at": "2026-11-16T06:41:13+00:00",
      "restored_at": "",
      "note": ""
    }
  ]
}
```

字段要点：

- `Change.before_exists` 区分“原来是 null”和“原来没有这个键”——恢复时前者写回 `null` 语义、后者删除键。
- `created_parents` 记录本次写入新建的中间父级（点分路径、由浅到深），恢复时用于清理空映射。
- `before_sha256` 等于备份文件的哈希；`backup_sha256` 冗余保存，便于审计。
- 单条坏记录读入时会被跳过但**不丢弃**：文件原样保留，避免“以为恢复了其实没记录”。
- 事务状态：`applied`（pending，可恢复）/ `restored` / `partially_restored`（有冲突）/ `failed`（写入失败，已回滚）。只有 `applied` 会被 `restore` 处理。

### 6.3 备份模型

- 备份文件是 `config.yaml` 的**原始字节**，不是重新序列化的 YAML；
- 创建使用排他模式（目标已存在就失败），写入后 `fsync`，并回读对比源文件哈希；
- 使用前 `verify_backup` 重新计算哈希，损坏即拒绝使用（`read_backup_bytes`）；
- 恢复与卸载都不删除备份；
- 事务与备份的对应关系：一条 `apply` 对应一个备份文件；失败事务也保留自己的备份。

## 7. 兼容性注册表

### 7.1 设计

- 每个已验证的 Hermes 版本对应 `hfk/compat/manifests/hermes-<version>.json`；
- `load_manifests()` 扫描目录下所有 `*.json`（坏 JSON 跳过）；`manifest_for(version_tuple)` 用**精确版本字符串**匹配；
- `check(version_tuple, version_text)` 的判定顺序：

```text
version 为 None            → supported=False, exact=False（无法确定版本，只读）
精确 manifest 命中         → supported=True,  exact=True
family 命中（"0.23.x"）    → supported=True,  exact=False（同族降级，要求重新预览）
其他                       → supported=False（只读）
```

- `supported` 决定 `apply/restore/uninstall` 是否被 `_assert_writable` 放行；`exact` 只用于展示与提示，**不阻止写入**。
- manifest 的 `supported_fields` 是**注册元数据**，不是运行时白名单：真正的写入白名单是 `hfk/schema.py::CATALOGUE`。两者的一致性由 `tests/test_schema.py::TestManifestCoverage` 强制（不一致测试立即失败）。

### 7.2 新增一个 Hermes 版本

1. **准备环境**：在目标版本上定位安装目录与 Home；确认 `install-stamp.json` 或 git tag 能给出准确版本。
2. **导出字段**：用目标版本的 Hermes venv、cwd = Hermes 仓库根运行 `scripts/probe_schema.py`，拿到 `DEFAULT_CONFIG` 中审批/确认相关路径与默认值。
3. **更新字段表**：与 `hfk/schema.py::CATALOGUE` 逐项对照，增删改 `FieldSpec`；每条都要有 `evidence`（源码文件 + 行号/键名）与 `effect` 说明；重新确认 A/B/C 分级与预设归属（预设只能含 A）。
4. **写注册清单**：新增 `hfk/compat/manifests/hermes-<version>.json`，字段至少包含 `manifest_version`、`hermes_version`（精确字符串）、`family`、`verified_at`、`verified_against`、`approval_entrypoints`、`supported_fields`（必须与 `CATALOGUE` 完全一致）、`mode_values`、`binary_mode_values`、`hot_reload`、`known_issues`。
5. **做字节对照**：用目标版本的 `utils.atomic_roundtrip_yaml_update` 与 HFK 的 `update_key` 改同一字段，比较产物字节、改动行数与 YAML 1.1 引号语义（修改 `scripts/parity_probe.py` 里写死的路径后运行）。
6. **补测试**：`tests/test_schema.py` 会自动校验新清单与字段表一致；再为新版本特有的默认值/枚举补断言；跑 `python -m unittest discover -s tests -v` 与 `hfk selftest`。
7. **更新文档**：`README.md` 的字段表/兼容性声明、`USER_GUIDE.md` 的版本相关提示、本文件的注册清单说明。

### 7.3 新增一个字段

1. 在 `CATALOGUE` 追加 `FieldSpec`：`path`、`title`、`kind`、`default`、`values`/`minimum`/`maximum`、`risk`、`presets`、`effect`、`surfaces`、`evidence`、`notes`、`unit_seconds`。
2. 同步 manifest 的 `supported_fields`（测试会强制相等）。
3. 若为 B 类：`presets` 必须为空；若为 C 类：不能进 `CATALOGUE`，应放进 `BLOCKED_FIELDS` 并在界面解释原因。
4. 补 `validate_value` 的边界测试（错误类型、越界、bool 冒充 int）。
5. 更新 README 字段表与 `catalogue` 示例。

## 8. 本地服务与 Desktop 插件分层

```text
Desktop 插件（Electron renderer，只做 UI）
        │  读取 <HERMES_HOME>/hfk/service.json 得到 base_url + token
        │  HTTP + X-HFK-Token（仅 127.0.0.1）
        ▼
HFK Core 服务（hfk/service/app.py，独立 Python 进程）
        │  固定路由 → facade.HFK
        ▼
facade → engine（patch / transaction / backup） → config.yaml
        │
        └─ 日志 <HERMES_HOME>/hfk/logs/hfk.log（无令牌、无秘密）
```

分层理由：Desktop 插件运行在 Electron renderer 沙箱里，不适合做“多文件写入 + 备份 + 事务 + 回滚”这类长事务；把写入集中到 Python core，只有一个进程需要遵守事务约定，插件只消费 HTTP 视图。

服务约束（`hfk/service/app.py`）：

| 约束 | 实现 |
|---|---|
| 只监听回环 | `ThreadingHTTPServer(("127.0.0.1", port), …)` |
| 每次启动随机令牌 | `secrets.token_urlsafe(32)`；`service.json` 0600 供本机插件读取 |
| 认证 | `X-HFK-Token`，`secrets.compare_digest` 常数时间比较；`/api/health` 例外（无需令牌） |
| DNS rebinding 防护 | `Host` 必须是 `127.0.0.1` / `localhost` / `::1`，否则 403 |
| 无命令执行 | 路由是固定白名单（见用户指南 2.11），没有 exec 端点 |
| 请求体上限 | 1 MiB（只接受字段路径与标量值） |
| 日志脱敏 | 只记路由、错误 code、事务 ID、字段计数；不记令牌 |
| CORS | 允许 `null` / `file://`（Electron renderer）与 `http://127.0.0.1*` / `localhost*` |

插件侧（运行期位置 `<HERMES_HOME>/desktop-plugins/hfk/plugin.js`，没有 manifest 文件，不属于 Python 包，也不参与其构建）只负责展示与触发；`uninstall` 会删除这个目录（`--keep-plugin` 保留）。插件的 HTTP 契约由 `tests/test_hfk.py::TestService` 覆盖（令牌、401、apply 返回 `transaction_id`），UI 本身不在本仓库的单元测试范围内。

## 9. 依赖方向

```text
cli ────────────────┐
service/app ────────┼──► facade ──► engine/patch ──► config/{yaml_io,keypath}
                    │                 │              ▲
                    │                 ├──► engine/{backup,transaction}
                    │                 ├──► schema
                    │                 ├──► compat
                    │                 └──► env
                    └──► selftest ──► testing（仅测试路径）
```

硬规则：`engine/**` 不导入 `cli`/`service`；`schema`/`compat`/`config` 不导入 `engine`；任何模块都不导入 Hermes。
