# hermes-toolkit

[![CI](https://github.com/damiyangguang-ai/hermes-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/damiyangguang-ai/hermes-toolkit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)](#)

围绕 **Hermes Agent** 的三个工具，合在一个仓库里：

| 目录 | 做什么 |
|---|---|
| `hermes-freedom-kit/` | 审批策略补丁器：审计、应用、回滚 Hermes 的 `approvals.*` 配置（含桌面插件） |
| `secresearch/` | 授权范围内的安全研究：被动侦察、授权闸门、本地靶场验证、证据、修复前后报告、自主任务、MCP server |
| `exprdiag/` | 只读诊断：Hermes 提示层是否存在内容限制、跨层冲突、拒绝归因、模型能力矩阵 |

## 边界（请先读，再判断这个项目是否适合你）

- **`hermes-freedom-kit` 只改审批策略**（"执行危险命令前要不要问你"），
  不改模型的内容策略，也不会让被拒绝的对话变成可以被回答。
- **`secresearch` 默认拒绝**：没有授权记录就不跑任何主动测试。
  授权记录只能由人在 CLI 里建立，**agent 无法给自己授权**。
  请只对你拥有或已获书面授权的资产使用。
- **`exprdiag` 只读**：回答"哪一层在拒"，不负责让被拒内容通过。
  它显式排除了露骨内容探测、真实公众人物性化、越狱提示词测试。

## 快速开始

```bash
git clone <repo>
python -m pip install "ruamel.yaml>=0.18,<0.19"

cd hermes-freedom-kit && PYTHONPATH=$PWD python -m unittest discover -s tests -v
cd ../secresearch      && PYTHONPATH=$PWD python -m unittest discover -s tests -v
cd ../exprdiag         && PYTHONPATH=$PWD python -m unittest discover -s tests -v
```

## 测试规模

| 子项目 | 用例 |
|---|---|
| hermes-freedom-kit | 45 单元测试 + 29 项自检 + 26 项插件渲染冒烟 |
| secresearch | 39 单元测试 + 19 项 MCP 协议测试 |
| exprdiag | 20 单元测试 |

## 许可

MIT
