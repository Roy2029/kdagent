# 更新日志

KDAgent 版本对应档位里程碑（版本路线见 `docs/技术规格/00-总览与路线图.md`，docs 本地维护不随仓库分发）。

## [0.5.2] - 2026-08-27 — M1 记忆实测修复批（模型端读不到记忆的真根因）

### 记忆修复（M1 闭环）
用户实测：项目级 + 全局级「用户偏好」都记录了称呼约定，但新会话询问称呼不按约定回答。0.5.1 排查结论只验证了「注入链路正常」，漏了**模型端能否读到指针指向的文件**——本轮实测复验定位到三层真根因：

- **相对指针解析错**：`index_markdown()` 注入的索引行是 `](用户偏好.md)` 相对路径，模型 ReadFile 以 work_dir 为基准解析到 `{work_dir}/用户偏好.md`（不存在，实际在 `.kdagent/memory/` 下）→ 读不到。修复：注入前把相对指针补全为**绝对路径**（`store.py`，正则 `\]\(([^()\s]*?\.md)\)` 按 scope 根拼全）
- **全局记忆在沙箱外**：全局记忆绝对路径（`~/.kdagent/memory/…`）在 work_dir 外，即使补全绝对路径也会被 L2 沙箱拦成 HITL。修复：`build_permission_checker` 加 `extra_roots=[~/.kdagent/memory]`（`cli.py`）
- **PathSandbox work_dir 未进 roots（重大 bug）**：沙箱 docstring 明确「允许目录：项目根（work_dir）+ 系统临时目录 + 白名单」，但实现只 append 了传入 roots 和 tempdir，**work_dir 漏加** → default 模式下**所有项目内文件**（含项目记忆）都被 L2 拦成 ask。此 bug 此前被测试掩盖（pytest tmp_path 恰在系统临时目录下放行）。修复：`__init__` 内把 `self._work_dir` 并入 roots（`sandbox.py`）
- **reminder 文案**：标注「指针已是绝对路径，直接作为 ReadFile 的 path 参数」（`agent.py`）

### 验收基线
738 passed + 5 skipped · mypy 93 源文件 · ruff 干净

## [0.5.1] - 2026-08-23 — 实测反馈修复批次（N1-N3 + R2/U1-U3/U5 + M1 排查）

### 权限与交互修复
- **N1 弹窗位置**：HITL/确认弹窗改贴输入框上方（`align: center bottom` + `margin-bottom`），不再居中遮挡 Chat 最新消息——对照上下文判断允许/拒绝
- **N2 权限模式持久化**：`/permissions` 切换落盘 `{项目}/.kdagent/permissions.mode`，cli 启动读回、优先于 `config.permissions.mode`，重启不重置回 default
- **N3 acceptEdits 只读免弹**：D10 命令级只读判断落地——acceptEdits 下 Bash **单条**只读命令（grep/find/ls/cat…）自动放行；含重定向/管道/命令组合仍按 L4 shell=ask（规则优先、黑名单先行不变）
- **R2 黑名单漏拦根因**：Agent 执行 `rm` 时给路径加双引号（`rm "/mnt/c/…"`），原正则要求 `rm ` 后直接跟路径，引号破坏匹配 → 挂载点/盘符删除全部漏拦。正则路径前容忍可选引号 `["']?` + 结尾字符类含引号 + `\b` 防误伤；带引号/不带引号/单引号/盘符路径均有测试覆盖
- **U1 工具区滚动+顺序**：容器改 `VerticalScroll`（展开详情可滚动）；摘要加全局递增序号 `#N`，调用顺序可辨
- **U2 Shift+Enter 换行**：`compat.py` 对 VK_RETURN+Shift 输出 CSI-u `\x1b[13;2u`（Textual 解析为 shift+enter），换行后 TextArea 自动长高（4→10 行）
- **U3 选单焦点**：`/session list` 初始焦点固定列表顶部 = 最新会话项（原按 current_sid 定位，切回旧会话后焦点落底）
- **U5 删当前会话**：`/session delete <当前sid>` 自动新建并切换会话，UI 历史清空、继续发消息落新会话

### 排查结论（仅验证注入链路；模型端读文件路径问题见 0.5.2）
- **M1 记忆"不生效"**：注入链路正常（`_assemble_payload` 注入 582 字符索引、Agent 主动 ReadFile 读记忆文件）；答不出 `pytest` 是因为记忆里无该条目——静默记忆写入有双门槛（≥10min + ≥20K 增量），显式"请记住"不触发落盘；正确用法是 `/memory add` 显式写入。**后续实测复验发现模型端另有根因**（索引相对指针 + 沙箱拦截，见 0.5.2）

### 验收基线
738 passed + 5 skipped · mypy 93 源文件 · ruff 干净

## [0.5.0] - 2026-08-16 — M5 生产级：SubAgent + Worktree + 评估 MVP

### 主里程碑（M5-a → M5-e）

| 里程碑 | 内容 |
|---|---|
| M5-a SubAgent 体系 | `Agent≈Tool` 统一入口 + 工具过滤四层（防递归/嵌套/越权）+ Task 工具集（TaskList/Get/Create/Update）+ 内置 4 Agent（Explore 只读 / Plan / general-purpose / Verification 默认关）+ Fork（继承父对话无条件后台） |
| M5-b Worktree 空间隔离 | slug 白名单防路径遍历 → `git worktree add` 空间隔离 + 显式 cwd 模式 + fail-closed 清理（有变更保留不丢成果）+ `/worktree` 命令 |
| M5-c 后台 + worktree | 后台任务生命周期（完成/失败 `<task-notification>` 注入）+ worktree 创建后设置（.worktreeinclude 复制 + 大依赖软链） |
| M5-d 命名 Agent | SendMessage 命名投递 + 消息循环串行消费（FIFO，多消息续跑）+ Fork 命名 |
| M5-e 评估 MVP | `kdagent eval` CLI：封史副本（git archive 单提交防作弊）→ 隔离执行 → 补丁提取 → 判分双轨（真实测试 / gold 相似度）→ 失败归类五类 |

### M5 遗留增强（D53-D83，31 块）速览

- **Harness 测试闭环**（D53）：TestRunner 工具（current/worktree/temp 三沙箱）+ TestingEvent + 规则量化四规则（先读后编辑/失败必重跑/禁碰测试/判据写作）+ 测试基建探测 + 常驻铁律
- **检查点与反思**（D54/D57/D58）：双层检查点（声明驱动 + 行为观察兜底）→ Replan 接入（断路器反复受阻引导换路）→ 声明 vs 行为自动核验（判据机械分类器）
- **错误模式沉淀**（D59）：写失败 → 事件驱动客观诊断（7 类）→ feedback 记忆自动复用
- **可观测性**（D55/D60/D68-D72/D80）：tool span 补 input/output 埋点 · eval 标记 contextvar 并发隔离 · compact/L2 压缩成本 span · metrics 聚合纯函数 · `/metrics` 面板 · 判分后回填 trace 判定 · **OTLP 接口实装**（标准库最小实装，protojson 编码）
- **评估体系完善**（D61-D67/D73/D81/D82）：复核界面 CLI + TUI 报告屏 · 复测对比（diff_runs/metrics_by_run）· 并发跑批 · 题序稳定排序 · 估算成本（计价表）· **P2P 保护判分**（F2P 全过 + P2P 无损坏才算 resolved）· **gold 校验**（环境失效题剔除）
- **测试 UI 三态渲染**（D74）：passed✓/failed✗/regression⚠
- **工具/Hook/MCP 补全**（D75-D77）：Hook 子 Agent 生效（共享引擎）· GitRevert 精确回退 · MCP 外部内容来源标注
- **SubAgent 运行时**（D78/D79）：子 Agent 挂父 trace（跨 trace 父子链可重建）· adoptRunning 前台切后台（超时/取消自动转后台继续）
- **计价配置化**（D83）：`cost:` 段按 provider 取价目（T5-1 机制，数值待标定）

### 验收基线

712 passed + 5 skipped · mypy 93 源文件 · ruff 干净

## [0.4.0] - 2026-08-16 — M4 好用档：静默记忆 + MCP + Skill

### 新增
- **静默记忆**（`08`）：MEMORY.md 索引注入式静默读（无向量/无 side-query）+ 结构化 JSON 操作集静默写（双门槛节流：≥10min + ≥20K 增量）+ Dreaming 整理（提取 → 去重 → 合并 → 归档，后台调度）
- **MCP 工具桥**（`09`）：官方 Python SDK 客户端 + 启动即后台连接（懒连死循环规避）+ 工具延迟加载跨厂商（四步延迟，改 reminder 不改 system 保前缀缓存）+ `/mcp` 命令
- **Skill 两阶段**（`09`）：轻量注册 → 按需 LoadSkill（改 reminder 不改 system）+ 三级优先级（项目>用户>内置）+ 自动注册 `/name` 命令 + `skill-creator` 内置
- **/memory 命令**：概要/列表/add/delete/clear
- **MEMORY.md 记忆管理**：`memory/*.md` 文件是唯一真相源

### 验收基线

368 测试全绿

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
