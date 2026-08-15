# KDAgent

类 Claude Code 的 Coding Agent（Python 3.11+ / Textual）。

- **状态**：阶段 8 实现期 · **M2 能用档完成**（v0.2.0）｜M1 能跑闭环 v0.1.0 → M2 三层压缩 + 可观测性
- **更新日志**：[CHANGELOG.md](CHANGELOG.md)
- 技术规格与路线图：`docs/技术规格/`（本地维护，未纳入仓库分发，主文档 `00-总览与路线图.md`）

## 能力

### 能跑（v0.1.0 · M1）
- ReAct Loop 引擎 + OpenAI 兼容（DeepSeek 主）/ Anthropic Messages 双 provider
- 7 内置工具：ReadFile / WriteFile / EditFile / Glob / Grep / Bash / TodoWrite
- 会话持久化（JSONL）+ Textual TUI + Slash 命令系统 + 中文 IME 兼容（conhost）
- 验收：核心 demo 自主写 http 服务器 → 编译 → 启动 + curl 验证 → 收尾落盘

### 能用（v0.2.0 · M2）
- **上下文三层压缩**：L1 大结果落盘、L2 在线摘要（经济性决策）、L3 Auto-Compact（9 部分摘要 + 近期原文保留 + 文件/todo 快照重灌 + 熔断/强制/紧急兜底）
- **可观测性**：Session/Trace/Span 落盘 + 脱敏 + OTel 接口预留
- **/compact**：手动压缩（前后 token 对比、带参保留重点，与自动共用 L3）
- **状态栏**：窗口占用实时显示（`tokens: 45,230/200k`）
- 会话恢复超限自动压缩 + todo 快照重灌

## 开发

```bash
uv sync               # 安装依赖 + dev 工具链
uv run pytest         # 测试（223 passed / 4 skipped）
uv run mypy src       # 类型检查（strict + warn_unreachable）
uv run ruff check .   # lint
uv run kdagent --version
```
