# 更新日志

KDAgent 版本对应档位里程碑（版本路线见 `docs/技术规格/00-总览与路线图.md`，docs 本地维护不随仓库分发）。

## [0.3.0] - 2026-08-16 — M3 可控档：五层权限 + Hook 引擎 + /permissions

### 新增
- **五层权限纵深防御**（`06`）：L1 危险命令黑名单（bash/pwsh/cmd 三套）→ L2 路径沙箱（symlink 解析 + Windows 大小写不敏感）→ L3 权限规则（YAML：`Bash(git *)`，deny>ask>allow）→ L4 模式矩阵（default/acceptEdits/plan/bypassPermissions × read/write/shell）→ L5 HITL 审批弹窗
  - **敏感路径禁写**（§3.8）：config/permissions/skills 系统文件绝对禁写，bypassPermissions 也不豁免（模型自改配置=提权）
  - **拒绝不终止 Loop**：deny/拦截 → is_error 结果进历史，模型下轮自行调整
  - **「始终允许」学习**：allow_always → 追加 `{项目}/.kdagent/permissions.local.yaml` 本地规则
- **Hook 引擎**（`06` §3.10）：11 事件（session/turn 生命周期 + pre/post_tool_use + startup/shutdown + error + compact）；条件语法 `==/!=/=~/~=` + `&&/||`；动作 prompt 注入（同步）/ command / http（后台）；`pre_tool_use` 唯一可拦截 + 短路；错误只记日志不中断主流程
- **/permissions 命令**（`05`）：查看/切换权限模式；状态栏显示当前权限模式
- **可观测性联动**（`07`）：`permission.check` span（effect/verdict 入属性）+ `hook.run` span（event 入属性）

### 变更
- `config.py` 升级为三源 YAML 合并（用户级 → 项目级 → 本地级）：`permissions.mode` 默认模式、`hooks` 列表
- 新增依赖：`pyyaml`（dev：`types-PyYAML`）
- `Agent._exec_one` 集成裁决链：checker 存在时接管 require_confirm（无 checker 保留 M1/M2 Y/N 行为）

### 修复
- Hook prompt 注入改为同步执行、command/http 后台调度（生命周期提示词确定性）
- 黑名单正则：`del /f /q C:\` 等多旗标命令正确命中

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
