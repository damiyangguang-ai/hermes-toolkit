# ExprDiag · Expression Engine 诊断

对应《HFK改进》Expression Engine 第 1–4 条 + 第 5 条（**有界版**）。

一句话：**它回答"是哪一层在拒"，不负责让被拒内容通过。**

## 命令

```powershell
$py = "<PYTHON>"
cd <WORKSPACE>\ExprDiag
$env:PYTHONPATH = (Get-Location).Path

# 1) 逐层检查提示词与内容限制（只读）
& $py -m exprdiag diag --out out\diag.md

# 2) 判断一次拒绝发生在哪一层
& $py -m exprdiag classify --text "error: content_filter triggered"
& $py -m exprdiag classify --text "抱歉，我不能协助" --sent

# 3) 模型能力矩阵（先用 mock 验证逻辑）
& $py -m exprdiag compat --mock

# 4) 真实模型（需要你自己的密钥环境变量；本工具不读任何配置文件里的密钥）
$env:MY_API_KEY = "..."
& $py -m exprdiag compat --provider "厂商名|https://api.example.com/v1|模型名|MY_API_KEY"

# 5) 看探测了什么、排除了什么
& $py -m exprdiag probes
```

## 第 1–4 条：诊断做什么

| 文档条目 | 实现 |
|---|---|
| Roleplay Recognition | 扫描文本，区分"内容限制句式"与"人设描述"，不把「性格」误判为「性」 |
| Persona Inspection | 逐层读 SOUL.md / 角色卡 / 系统提示词 / profile / 插件，**每条命中都带 file:line** |
| Prompt Conflict Detection | 同一主题在不同层出现相反指令 → 报**候选**冲突（不武断下结论） |
| Refusal Diagnostics | 按证据归因到 `provider` / `local` / `model_judgment` / `unknown`，**缺证据就标 Unknown** |

### 拒绝归因的判据

| 证据 | 归因 | 置信度 |
|---|---|---|
| 响应里有 `content_filter` / `data_inspection` / `moderation_blocked` / `guardrail` | **provider** | high |
| 有 `blocked_by_hardline` / `approval_required` 等本地标记 | **local** | high |
| 确认请求已发出、但无策略错误码 | **model_judgment** | medium |
| 两者都没有 | **unknown** | low（不猜） |

## 第 5 条：能力矩阵 —— 测什么、不测什么

**测**（合法创作能力）：

| 任务类 | 说明 |
|---|---|
| 成年角色恋爱对话 | 含蓄，靠动作与停顿 |
| 暧昧/调情（非露骨） | 克制、留白 |
| 成熟题材（成年人关系） | 情感伤害、坦白，非露骨 |
| 长上下文人设连续性 | 依据设定保持人物一致 |
| 中文语感与称呼 | 约束句式与用词 |
| **边界检查** | 未成年人相关请求——**期望被拒绝**，拒绝才是正确行为 |

**不测**（报告里显式列出理由）：

| 排除项 | 理由 |
|---|---|
| 露骨性描写探测 | 唯一用途是筛「哪家供应商审核最松」，属规避策略 |
| 真实公众人物性化内容 | 涉及真实在世人名誉权与深度合成规定 |
| 越狱提示词有效性测试 | 属规避工具 |

矩阵只回答"该类**创作任务**是否可用"，**不给出"支持度"分数**，也**不衡量"能写多露骨"**。
被拒会区分成「被拒（模型判断）」与「被拦截（provider 策略）」两列，不合并。

## 在真实机器上跑出来的结论

对 `<HERMES_HOME>` 的只读诊断：

```
共检查 14 个提示层
未在任何本地提示层发现内容限制句式 → 本地层不是拒绝来源
```

含义：**SOUL.md、Hermes 系统提示词、HFK 插件里都没有内容限制条款**。
也就是说，被拒不可能来自本地配置——只能来自 provider 策略或模型判断。
（剩余未知见报告第 5 节，不装懂。）

## 开发中修掉的假阳性/假阴性

这些不是"顺手优化"，而是**会让报告结论反向**的错误：

| 问题 | 类型 | 后果 |
|---|---|---|
| 只扫 `agent/system_prompt.py`（拼装器） | 假阴性 | 会得出"本地没有任何限制"的错误结论 |
| `minor` 命中 `(major, minor)` 版本元组 | 假阳性 | 把 Gemini 适配器误报成未成年条款 |
| `consent` 命中工具调用同意 | 假阳性 | 把压缩器误报成内容政策 |
| 任何 400 字以上 docstring 都算提示层 | 假阳性 | 层清单塞进 40 个业务代码文件 |
| mock 用 `"露骨" in prompt` 判定 | 假阳性 | 把"不要露骨描写"的提示词判成被拦截 |

## 边界

- **只读**：不修改任何文件，不写配置，不删东西。
- **不读密钥**：真实模型只从环境变量取 key，且不写入报告。
- **不做规避**：不产出越狱提示词、不测试审核松紧、不帮内容通过。
- 本地层若确有限制，可被人为修改；provider 侧策略不在本地可控范围。

## 测试

```powershell
& $py -m unittest discover -s tests -v
```

20 个用例，覆盖：假阳性（性格/版本号/工具同意）、假阴性（拼装器 vs 真提示词容器）、
跨层冲突检测、四种拒绝归因、矩阵（可用/被拦截/安全行为正确/不安全模型被标出）、
以及"排除项必须写进报告"。
