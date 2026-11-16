# SecResearch · HFK Security Research Engine

授权范围内的安全研究工具：**被动侦察 + 授权闸门 + 本地靶场验证 + 证据 + 修复前后报告**。

一句话原则：**没有授权记录，主动测试一律不启动。**

## 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 被动侦察：响应头、技术栈、TLS、版本线索 | 不做端口扫描、子域枚举、目录爆破 |
| 授权记录管理（所有者/证明/时间窗/范围/禁止项/停止条件） | **不碰**任何无授权的第三方目标 |
| 本地靶场真实验证：SQL 注入 / IDOR / 反射型 XSS / SSRF / 信任客户端角色 / 响应头缺失 | 不做真实环境漏洞利用、不提权、不横向移动 |
| 证据落盘（请求/响应/判定，凭据脱敏） | 不把猜测写成"已确认漏洞" |
| 修复前后对比报告（CWE 对应） | 不声称能绕过任何平台策略 |

## 快速开始

```powershell
$py = "<PYTHON>"
cd <WORKSPACE>\SecResearch
$env:PYTHONPATH = (Get-Location).Path

# 端到端闭环：启动靶场 → 侦察 → 发现 → 修复 → 复测 → 出报告
& $py -m secengine demo --out out\report.md
```

产出：

```
out\report.md              修复前后对比报告
out\evidence\before\*.json 修复前证据（含请求/响应）
out\evidence\after\*.json  修复后证据
out\authorizations.json    本次用到的授权记录
```

## 命令

| 命令 | 说明 |
|---|---|
| `secengine task demo` | **自主任务闭环**：拆解→执行→校验→交付（本机靶场） |
| `secengine task run --target --base-url` | 对已授权目标自主执行 |
| `secengine task resume --task-id` | 从 checkpoint 恢复 |
| `secengine task list` | 列出任务记录 |
| `secengine demo` | 本机靶场修复前后对比闭环 |
| `secengine recon <url>` | 被动侦察，不需要授权 |
| `secengine authz add --target --owner --proof` | 建授权记录 |
| `secengine authz list` | 看记录及是否有效 |
| `secengine authz check --target --scope` | 检查某目标某类型是否已授权 |
| `secengine check --target --base-url --kind sqli\|idor\|xss\|ssrf\|privesc` | 主动测试，**需有效授权** |

主动测试被拒时返回码为 `3`，并说明拒绝原因。

## 授权记录字段（对应文档第二模块）

```json
{
  "target": "127.0.0.1:8000",
  "owner": "资产所有者",
  "proof": "授权证明：工单号 / 邮件摘要 / 自建靶场说明",
  "starts_at": "2026-11-16T08:00:00+00:00",
  "ends_at": "2026-11-16T12:00:00+00:00",
  "scopes": ["sqli", "idor"],
  "forbidden": ["任何非本机目标"],
  "stop_conditions": ["发现目标不是授权资产立即停止"]
}
```

以下任一情况一律判定为无效：缺所有者、缺证明、缺范围、时间窗未开始或已过期。

## 自主任务执行（文档最高优先级模块）

给一个目标，它自己拆解、执行、校验、交付，中途不逐条询问：

```powershell
& $py -m secengine task demo --out out-task
```

```
[1/4] 自动拆解：8 步 → recon, headers, sqli, idor, xss, ssrf, privesc, report
[2/4] 自主执行（不逐条询问）
      #0 recon     done     ok
      #1 headers   done     vulnerable
      #2 sqli      done     vulnerable
      ...
[3/4] 生成报告并做完成校验（重新读磁盘，不看自述）
      校验：通过
[4/4] 交付：报告 + 账本 + 证据
```

关键点：

| 文档要求 | 实现 |
|---|---|
| 自动拆解 | `plan()` 按**授权范围**决定排哪些步骤（没授权的主动步骤根本不排） |
| 不逐条下令 | `run()` 一次跑完；只在越权/高影响时暂停 |
| 真实执行记录 | `out/tasks/<task>.json` 记录每步的工具名、参数、尝试次数、结论、证据 id |
| **不凭声称校验** | `verify()` **重新读磁盘**：证据文件是否存在、报告章节是否齐全、CWE 是否来自真实证据 |
| 失败重试 | 每步最多 `max_attempts` 次（默认 2） |
| 暂停/恢复 | 越权 → 该步 `blocked`、任务转 `paused`；checkpoint 支持 `task resume` |
| 不另造框架 | 只做「拆解 → 调用既有检查函数 → 记录 → 校验」，不做第二套 agent loop |

完成校验会**主动失败**的两种情况（有测试覆盖）：

- 把证据文件从磁盘删掉，账本仍声称存在 → 校验报"不在磁盘上"
- 报告缺少必需章节 → 校验报"缺少必需章节"

## 本地靶场

`lab/vulnapp.py` 是一个**故意有漏洞**的本机服务（只监听 127.0.0.1）：

| 漏洞 | 位置 | CWE |
|---|---|---|
| SQL 注入（认证绕过） | `/login` 字符串拼接 | CWE-89 |
| 越权访问 IDOR | `/profile?id=N` 无归属校验 | CWE-639 |
| 反射型 XSS | `/search?q=` 不转义输出 | CWE-79 |
| SSRF | `/fetch?url=` 服务端代取任意 URL | CWE-918 |
| 信任客户端角色越权 | `/admin` 相信 `X-Role` 头 | CWE-285 |
| 安全响应头缺失 | 所有响应 | CWE-693 |

```powershell
& $py lab\vulnapp.py --port 8000          # 漏洞版
& $py lab\vulnapp.py --port 8001 --fixed  # 修复版（用于对比）
```

种子账号：`alice/pw-alice`、`bob/pw-bob`、`admin/pw-admin`。

## 检测判据（为什么不是"看响应像不像"）

- **SQLi**：用**错误密码** + 注释载荷登录，若仍返回 `ok: true`，说明密码条件被注释掉 → 注入成立
- **IDOR**：不带任何身份信息请求他人 `id`，若返回他人资料 → 越权成立
- **XSS**：原始 `<script>` 载荷在响应正文里**未转义**出现（若变成 `&lt;script&gt;` 则视为已编码，判为不存在）
- **SSRF**：传入一个**修复后本不该允许**的内部地址（`/profile?id=2`），若服务端代取并回显 → 成立
- **越权**：不带有效会话，只加 `X-Role: admin` 头，若拿到管理员面板 → 权限判断建立在可伪造信息上
- **响应头**：缺失即记录；`Server` 只有**带版本号数字**才算信息泄露

> 判据里的两个反例都是真实踩过的坑：SSRF 一开始拿 `/healthz` 当证据，而它恰在修复版白名单里，于是把"已修复"误判成"仍存在"。

## 限制（明确标注，不装懂）

- **未实现 CVE 关联查询**：需要外部漏洞库接口，本工具不联网查询、不猜编号 → 该项标 Unknown
- **未接入 Hermes Agent（文档第六模块）**：当前是独立 CLI，未注册为 Hermes 工具
- 未做存储型 XSS、盲注、时序注入、OAuth/OIDC 流程深度测试
- 只验证"是否可复现"，不构造利用链、不做影响扩大

## 测试

```powershell
& $py -m unittest discover -s tests -v
```

39 个用例，覆盖：授权闸门（无记录/过期/范围不符/目标不符/缺证明/新三类同样受闸门约束）、六类漏洞"必须检出"与"修复后必须消失"、脱敏、报告计数、自主任务（拆解/执行/账本/暂停/恢复/完成校验会因证据消失而失败）、以及**端到端验收**。
