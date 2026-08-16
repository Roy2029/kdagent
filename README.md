# KDAgent

类 Claude Code 的 Coding Agent（Python 3.11+ / Textual）。

- **状态**：阶段 8 实现期 · **M5 生产级完成**（v0.5.0）｜M1 能跑 v0.1.0 → M2 能用 v0.2.0 → M3 可控 v0.3.0 → M4 好用 v0.4.0 → M5 生产级 v0.5.0
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

### 好用（v0.4.0 · M4）
- **静默记忆**：MEMORY.md 索引注入式静默读（无向量）+ 双门槛静默写（≥10min + ≥20K 增量）+ Dreaming 整理后台调度
- **MCP 工具桥**：官方 SDK + 启动即后台连接 + 工具延迟加载跨厂商（改 reminder 不改 system，保前缀缓存）
- **Skill 两阶段**：轻量注册 → 按需 LoadSkill；三级优先级（项目>用户>内置）；自动注册 `/name` 命令
- **/memory /mcp /skills**：记忆/工具/技能管理命令

### 生产级（v0.5.0 · M5）
- **SubAgent 体系**：`Agent≈Tool` 统一入口 + 工具过滤四层 + Task 工具集 + 内置 4 Agent + Fork（继承父对话）
- **Worktree 空间隔离**：slug 白名单 + 显式 cwd + fail-closed 清理 + `/worktree`
- **后台任务**：生命周期 + `<task-notification>` 注入 + worktree 创建后设置；**命名 Agent**（SendMessage 多消息续跑）
- **评估 MVP**：`kdagent eval` 封史防作弊 → 隔离执行 → 判分双轨 → 失败归类五类 + 复核界面（CLI/TUI）+ 复测对比 + 并发跑批

**M5 遗留增强（31 块）速览**：
- Harness 测试闭环：TestRunner（三沙箱）+ 规则量化四规则 + 测试基建探测
- 双层检查点 + Replan 引导 + 声明 vs 行为自动核验 + 错误模式沉淀（feedback 记忆）
- 可观测性：compact/L2 压缩 span 埋点 · `/metrics` 面板 · trace 判分回填 · **OTLP 接口实装**
- 评估：P2P 保护判分（F2P 全过 + P2P 无损坏才算 resolved）· gold 校验（环境失效剔除）· 估算成本（计价表按 provider 配置化）
- SubAgent：子 Agent 挂父 trace · adoptRunning 前台切后台（超时/取消自动转后台）
- 补全：Hook 子 Agent 生效 · GitRevert 精确回退 · MCP 外部内容来源标注 · TestingEvent UI 三态

## 开发

```bash
uv sync               # 安装依赖 + dev 工具链
uv run pytest         # 测试（712 passed / 5 skipped）
uv run mypy src       # 类型检查（strict + warn_unreachable）
uv run ruff check .   # lint
uv run kdagent --version
```
