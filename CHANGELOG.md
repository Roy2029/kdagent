# 更新日志

KDAgent 版本对应档位里程碑（版本路线见 `docs/技术规格/00-总览与路线图.md`，docs 本地维护不随仓库分发）。

## [0.2.0] - 2026-08-16 — M2 能用档：上下文三层压缩 + 可观测性 + /compact

### 新增
- **上下文三层压缩**（`01`）
  - L1：大工具结果落盘 + ReadFile 读回闭环（offset/limit 读段、防二次落盘）
  - L2：在线语义摘要（三门槛经济性决策：作用域 / 信息密度 / 盈亏平衡）
  - L3 Auto-Compact：9 部分结构化摘要 + 近期原文保留（tool 配对不切断）+ 文件/todo 快照重灌；独立预算（auto 熔断只关自动路径 / force 耗尽抛 `ContextFullError` / 成功复位）；摘要超长 4 次重试兜底
- **可观测性**（`07`）：Session/Trace/Span 落盘（`{kdagent_dir}/obs/traces/{sid}/`）+ 脱敏 + OTel 导出接口预留；`debug.log_full_prompt` 全文日志开关
- **/compact 命令**（`05`）：与自动共用 L3 逻辑；前后 token 对比；`<5K` 提示无需压缩；带参=保留重点注入摘要
- **状态栏窗口占用**（`05`/`07`）：`tokens: 45,230/200k`（当前窗口占用/窗口上限），随 UsageEvent 与消息落盘实时刷新
- **会话恢复触发压缩**（`04`/`12`）：resume 超 `AUTO_COMPACT_TRIGGER` 自动压缩 + todo 快照以 `system-reminder` 重灌（去重）

### 变更
- `sessions/__init__.py` 改 PEP 562 懒加载，破除 compactor↔manager 循环导入
- `Agent.system_prompt` 公开只读属性（窗口估算取当前 prompt）

### 修复
- `_persist_history` 压缩后重写会话 JSONL 前懒建目录
- Textual App 命令属性不用 `_registry`（与 Textual 内部 CommandRegistry 同名冲突，启动即崩）

## [0.1.0] - 2026-08-15 — M1 能跑最小闭环

### 新增
- ReAct Loop 引擎：OpenAI 兼容（主，DeepSeek）+ Anthropic Messages（备）双 adapter，`MAX_ITERATIONS=50`
- 7 内置工具：ReadFile / WriteFile / EditFile / Glob / Grep / Bash / TodoWrite
- 会话管理：JSONL 持久化 + /session 切换 + 30 天过期清理
- Textual TUI：Chat/todo/tools 三区域 + 状态栏 + Y/N 确认弹窗 + 7 个 Slash 命令
- 中文 IME / conhost 终端兼容（M1-i 系列：传统输入层对齐、IME 二次修复、WSL 挂载路径解析）

### 验收
- 核心 demo（2026-08-15 用户实测）：自主 TodoWrite 规划 → Bash 写码 → gcc 编译 → 启动 + curl 验证 → 收尾落盘
