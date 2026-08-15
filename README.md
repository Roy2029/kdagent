# KDAgent

类 Claude Code 的 Coding Agent（Python 3.11+ / Textual）。

- **状态**：阶段 8 实现期 · **M3 可控档完成**（v0.3.0）｜M1 能跑 v0.1.0 → M2 能用 v0.2.0 → M3 权限五层 + Hook 引擎
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

### 可控（v0.3.0 · M3）
- **五层权限纵深防御**：L1 危险命令黑名单 → L2 路径沙箱 → L3 权限规则（`Bash(git *)`）→ L4 模式矩阵（default/acceptEdits/plan/bypassPermissions）→ L5 HITL 审批弹窗（允许/拒绝/始终允许）
- **敏感路径禁写**：config/permissions/skills 系统文件绝对禁写（bypass 也不豁免）；**拒绝不终止 Loop**（deny → is_error 进历史，模型下轮调整）
- **Hook 引擎**：11 事件 + 条件语法（`==/!=/=~/~=`、`&&/||`）+ prompt 注入/command/http 动作 + `pre_tool_use` 可拦截短路
- **/permissions**：查看/切换权限模式；状态栏显示当前权限模式
- **可观测性联动**：`permission.check` / `hook.run` span 入 trace

## 开发

```bash
uv sync               # 安装依赖 + dev 工具链
uv run pytest         # 测试（290 passed / 5 skipped）
uv run mypy src       # 类型检查（strict + warn_unreachable）
uv run ruff check .   # lint
uv run kdagent --version
```
