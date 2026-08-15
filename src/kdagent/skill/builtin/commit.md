---
name: commit
description: 分析 git 变更并生成符合仓库现有风格的 commit message 与提交
mode: inline
---

# 任务

分析当前 git 变更，生成 commit message 并提交。

## 步骤

1. 运行 `git status` 查看变更状态
2. 运行 `git diff` 和 `git diff --staged` 查看具体变更内容
3. 运行 `git log --oneline -10` 查看仓库现有 commit 风格，据此生成匹配的 message
4. 用 `git add <file>` 逐个添加相关文件（**不要** `git add -A`）
5. 执行 `git commit -m "<message>"` 提交

## 注意事项

- 严格遵循仓库现有 commit 风格（type/scope、语言、长度）
- 不要提交敏感文件（.env、凭据、密钥等）
- 变更过大时建议拆分成多个 commit
- 如果用户提供了额外说明，纳入 commit message

$ARGUMENTS
