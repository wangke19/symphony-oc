# OpenCode Symphony

个人开发者用的 Issue-Driven Agent 编排系统 — 基于 OpenCode + git worktree + gh CLI。

## 快速开始

```bash
# 1. 安装 opencode (≥ 1.17.7)
curl -fsSL https://opencode.ai/install.sh | sh

# 2. 配置 gh
gh auth login

# 3. 设置 upstream remote
git remote add upstream <fork-target-url>

# 4. 运行 bootstrap
python -m symphony_oc.bootstrap

# 5. 启动 orchestrator
python -m symphony_oc.orchestrator
```

## 架构

见 `2026-06-14-opencode-symphony-design.md`。
