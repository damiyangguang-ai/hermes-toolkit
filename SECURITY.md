# 安全说明

## 报告漏洞

请通过 GitHub 私有安全通告报告，不要开公开 issue。

## 本项目的安全设计

### 授权闸门（`secresearch`）

主动测试（SQLi / IDOR / XSS / SSRF / 越权）必须通过 `AuthorizationStore.assert_authorized()`：

- 缺目标 / 所有者 / 授权证明 / 时间窗 / 范围 → 拒绝
- 时间窗过期 → 拒绝
- 目标不匹配 → 拒绝
- **agent 无法创建授权记录**：MCP 层刻意不暴露该能力

请只对自有或已获书面授权的资产使用。

### 令牌文件权限（`hermes-freedom-kit`）

本地服务令牌写在 `<HERMES_HOME>/hfk/service.json`：

- Windows 上用 `icacls /inheritance:r /grant:r <user>:(R,W)` 收紧到当前用户
- 收紧后回读自检，读不到则恢复继承（避免把用户锁在门外）

### 不记录密钥

`exprdiag` 的真实模型适配只从环境变量取 key，不写入任何报告或磁盘。
