# MewCode 设计文档 · 索引与摘要

> 本目录共收录 **14 篇**文档，全部抓取自飞书 MewCode 课程系列，内容为 MewCode（一款类 Claude Code 的终端 AI 编程助手）从零搭建的完整设计理论与实现讲解。文档按教学顺序渐进，前后章节相互引用。
>
> 抓取时间：2026-08-04 ~ 2026-08-05

## 阅读指引（建议顺序）

| 顺序 | 主题 | 文档 |
| --- | --- | --- |
| 1 | 引擎基础 | [LLM API 与对话管理器](#llm-api-与对话管理器) |
| 2 | 工具层 | [Function Calling 与工具系统](#function-calling-与工具系统) |
| 3 | Agent 心脏 | [ReAct 范式与 Agent Loop](#react-范式与-agent-loop) |
| 4 | 行为质量 | [System Prompt 如何设计](#system-prompt-如何设计) |
| 5 | 安全层 | [五层纵深权限防御](#五层纵深权限防御) |
| 6 | 记忆层（会话内） | [上下文压缩与 Token 管理](#上下文压缩与-token-管理) |
| 7 | 工具生态 | [MCP 协议与开放工具生态](#mcp-协议与开放工具生态) |
| 8 | 交互层 | [Slash Command 命令框架](#slash-command-命令框架) |
| 9 | 交互层 | [Skill 可复用技能包](#skill-可复用技能包) |
| 10 | 工具层 | [Hook 生命周期钩子](#hook-生命周期钩子) |
| 11 | 引擎扩展 | [SubAgent 子任务分发](#subagent-子任务分发) |
| 12 | 安全层 | [Git Worktree 并行隔离](#git-worktree-并行隔离) |
| 13 | 记忆层（跨会话） | [跨会话记忆与会话持久化](#跨会话记忆与会话持久化) |
| 14 | 验收评测 | [给你的 Agent 跑一次真实评测](#给你的-agent-跑一次真实评测) |

> 注：原文中章节号存在少量前后不一致（如第 8 章、第 11 章、第 13 章引用），上表顺序依据正文相互引用关系推断，不保证与原文章节号完全一致。

---

## 引擎基础

### LLM API 与对话管理器

- **文件**：[理论学习_LLM_API与对话管理器.md](理论学习_LLM_API与对话管理器.md)（约 20 KB）
- **架构定位**：② 引擎层的 LLM 客户端与对话管理器，Agent 循环与子 Agent 的基础设施。

**摘要**
本章从零调通 Anthropic Messages API，并封装出可多轮对话的终端界面。重点讲解 Messages 格式中 user/assistant 交替规则与 content 永远是数组的设计、基于 SSE 的流式响应、Token 计费与 Extended Thinking，以及如何通过封装层屏蔽不同供应商协议差异。核心结论是：LLM API 无状态，多轮对话全靠客户端维护完整消息历史，因此需要一个对话管理器集中管理消息拼接规则（工具结果必须以 user 身份回传）。

**大纲**
- 概念 → 动手：调通 API、套上终端 UI 支持多轮对话
- Messages 格式：user/assistant 交替惯例、content 为内容块数组（text / tool_use）
- 流式响应：SSE 事件序列（message_start → content_block_start/delta/stop → message_delta → message_stop）；不同语言的流式原语
- 请求三个字段：system（角色+环境）、messages（对话历史）、tools（工具描述）
- Token：计费结构、输出比输入贵 5 倍、上下文随轮次线性增长
- Extended Thinking：thinking 块需带签名原样回传
- 封装核心原则：暴露领域语义、隐藏 SDK 细节，四字段配置（protocol/model/base_url/api_key）
- 对话管理器：统一消息构造规矩，为上下文压缩、回滚留扩展口
- 终端 UI：TUI 方案（上方对话区 / 下方输入框 / 底部状态栏），参考 Claude Code

---

### Function Calling 与工具系统

- **文件**：[理论学习_Function Calling与工具系统.md](理论学习_Function Calling与工具系统.md)（约 9 KB）
- **架构定位**：③ 工具层的核心组件，工具是 Agent 能力的边界。

**摘要**
本章讲解 Function Calling 的四步协作协议（告知工具 → 模型发起请求 → 本地执行并回传结果 → 模型继续），强调「模型负责决策、你负责执行」的安全边界。核心观点是工具描述（description）是工具系统最重要的部分，直接决定模型何时调、调哪个、参数怎么传。设计上要求工具接口不止「名字+执行」，还需携带只读/破坏性/并发安全/参数校验等元信息，为权限系统与并发控制提供依据。

**大纲**
- Agent 四要素（LLM + 工具 + 循环 + 反馈）缺工具则只是聊天机器人
- Function Calling 四步流程：tools 参数声明 → 模型输出 tool_use → 本地执行回传 tool_result（以 user 角色）→ 模型继续
- 工具描述是最值得打磨的部分：六要素（做什么 / 何时用 / 何时不用 / 参数约束 / 返回格式 / 与其他工具配合）
- 工具接口设计：name / description / inputSchema / execute / isReadOnly / isDestructive / isConcurrencySafe / category / validateInput

---

## Agent 心脏

### ReAct 范式与 Agent Loop

- **文件**：[理论学习_ReAct范式与Agent Loop.md](理论学习_ReAct范式与Agent Loop.md)（约 21 KB）
- **架构定位**：② 引擎层的 Agent 循环（ReAct Loop），整个架构的心脏。

**摘要**
ReAct 范式即「推理（Think）→ 行动（Act）→ 观察（Observe）」交替循环，Claude API 原生支持该模式（text = Think、tool_use = Act、tool_result = Observe）。Agent Loop 的本质就是一个 while 循环持续调 LLM 并拼接上下文，直到模型不再请求工具。本章给出四类必配停止条件、AgentEvent 事件流解耦设计、工具调用并发分批逻辑，以及用 Prompt 约束实现的 Plan Mode。

**大纲**
- ReAct 范式与 CoT / Act-only / Plan-then-Execute 对比；对 Coding Agent 最自然
- Agent Loop 核心：一个 while 循环 + 消息拼接（tool_result 以 user 发送、id 一一对应）
- 四种停止条件：模型主动 end_turn、50 次迭代上限、用户 Esc 取消、异常检测（工具不存在）
- AgentEvent 事件流：stream_text / tool_use / tool_result / turn_complete / loop_complete / usage / error；Agent 与 UI 完全解耦
- 状态机思维：每轮仅 CONTINUE / TERMINAL 两条路，便于扩展 NEED_CONFIRM、RATE_LIMITED
- 工具分批执行：按 isConcurrencySafe 划分并发批与串行批
- System Prompt 与环境信息（工作目录、OS，利于 Prompt Cache）
- Plan Mode：通过 Prompt 指令约束 + 权限系统兜底（plan 文件自动放行）

---

## 行为质量

### System Prompt 如何设计

- **文件**：[理论学习_System Prompt如何设计.md](理论学习_System Prompt如何设计.md)（约 33 KB）
- **架构定位**：横跨引擎层（组装管线）与工具层（工具描述），共同塑造 Agent 行为质量。

**摘要**
System Prompt 的本质不是教模型新能力，而是约束 LLM 的默认泛化倾向。本章把三行 prompt 展开为七个模块的生产级体系（角色设定、行为准则、工具使用指南、代码质量规范、安全边界、任务执行模式、输出风格），并给出 Prompt 组装管线：稳定内容放 system 字段利用 Prompt Cache、动态内容放 messages、工具描述放 tools。关键技巧包括关键规则双重强化、正面表述代替负面堆砌、核心指令放首尾，并系统分析了 Prompt 设计对 token 成本的三处影响。

**大纲**
- 三行 prompt 为什么不够：LLM 默认倾向（长、全面、多做事）在 Agent 场景下全是反模式
- 七个模块逐一拆解（每条指令对应纠正某个默认倾向）
- 组装管线：七个信息来源 → 三个字段；Prompt Cache 是字段划分的第一理由
- 动态指令注入：`<system-reminder>` 标签放进 messages，不破坏 system 缓存
- 工具描述也是 Prompt 工程：关键规则在 System Prompt 与工具描述中双重强化
- 常见陷阱：Lost in the Middle、指令冲突（声明优先级）、负面指令堆砌（改写正面）、只在一处说
- Prompt 与成本：system 稳定性决定缓存命中率、输出长度决定单轮成本、工具并行度决定总轮次

---

## 记忆层（会话内）

### 上下文压缩与 Token 管理

- **文件**：[理论学习_上下文压缩与Token管理.md](理论学习_上下文压缩与Token管理.md)（约 24 KB）
- **架构定位**：④ 记忆层的上下文管理组件，让 Agent 在有限 Token 窗口内长时间工作。

**摘要**
Token 消耗的核心问题是工具结果占比约 85%，是压缩的首要对象。本章给出两层压缩策略：第一层「大结果存磁盘」（单条超 50K 字符 / 单轮合计超 200K 字符时，完整内容落盘只留预览），几乎零损失零开销且天然适配 Prompt Cache；第二层「摘要旧消息、保留近期原文」（Auto-Compact，约 167K Token 触发，保留最近 1 万 Token 或至少 5 条原文）。配套三重视线保障：177K 强制压缩线、API 返回超长错误时的紧急压缩、连续 3 次失败熔断。核心哲学是「能轻则轻」。

**大纲**
- Token 消耗构成表：工具结果（文件/命令/搜索）≈ 85%
- 第 1 层：单条 >50K 字符落盘 + 预览；单轮并行结果合计 >200K 时挑最大存盘；写入即终态 → Prompt Cache 天然友好；读回文件不过度溢写
- 第 2 层 Auto-Compact：167K 触发阈值推导（200K − 20K 摘要预留 − 13K 安全余量）；固定值而非百分比的原因；20K 预留的上下限夹逼
- 摘要 Prompt：9 个结构化部分（用户消息尽量原文保留）；两阶段生成（<analysis> 草稿 → <summary> 正文）；两头堵禁止工具调用
- 压缩后恢复：保留近期原文 + 恢复最近访问文件（最多 5 个）/ 技能定义；会话记录路径防止模型编造细节
- 兜底保障：177K 强制压缩线、紧急压缩（prompt_too_long 时先压再重试）、连续 3 次失败熔断
- 手动 /compact：预防性压缩与话题切换
- 两层协作：第 1 层管入口（预防），第 2 层管累积（治疗）

---

## 安全层

### 五层纵深权限防御

- **文件**：[理论学习_五层纵深权限防御.md](理论学习_五层纵深权限防御.md)（约 31 KB）
- **架构定位**：安全层的权限系统，贯穿所有操作但不干预业务逻辑。

**摘要**
面对 Prompt 注入、越权操作、数据泄露三类威胁，MewCode 建立五层纵深防御：① 危险命令黑名单硬拦截 → ② 路径沙箱（解析符号链接防逃逸）→ ③ 细粒度权限规则（ToolName(pattern)，deny > ask > allow 裁决）→ ④ 权限模式（default / acceptEdits / plan / bypassPermissions）→ ⑤ HITL 人在回路。关键设计包括：权限被拒返回错误结果而非终止循环（让模型调整策略）、「始终允许」形成权限学习循环、应用层与 OS 级沙箱（seatbelt / bubblewrap+seccomp）双层联动（autoAllow）。

**大纲**
- 三种威胁模型：Prompt 注入（间接注入）、越权操作、数据泄露
- 第 1 层 危险命令黑名单：正则硬拦截（rm -rf /、mkfs、管道执行远程脚本等），仅对 Bash 生效
- 第 2 层 路径沙箱：resolveSymlinks 防符号链接逃逸；不存在文件检查父目录；默认允许项目目录 + 临时目录
- 第 3 层 权限规则：ToolName(pattern) 语法、各工具提取的匹配字段、三份规则文件合并后 deny > ask > allow（层级不参与裁决）
- 第 4 层 权限模式：四种模式决策矩阵（只读/写文件/Bash）
- 第 5 层 HITL：确认对话框、「始终允许」动态生成 allow 规则形成学习循环；Agent Loop 与 UI 的同步机制
- 决策链：上一层能决策就直接返回，不能决策才下放
- 被拒绝不终止循环：errorResult 进对话历史，模型下一轮自行调整
- OS 级沙箱：macOS seatbelt / Linux bubblewrap+seccomp；敏感路径禁写（config.yaml / permissions / skills）；默认断网；autoAllow 联动；/sandbox 三模式切换

---

### Git Worktree 并行隔离

- **文件**：[理论学习_Git Worktree并行隔离.md](理论学习_Git Worktree并行隔离.md)（约 28 KB）
- **架构定位**：安全层的文件系统隔离，补上安全层最后一块拼图。

**摘要**
后台子 Agent 与主 Agent 并行时共享文件系统会导致冲突，而 Git 分支只提供时间维度隔离。Git Worktree 实现空间维度隔离：同一仓库多独立工作目录、共享 .git、历史统一。MewCode 设计 WorktreeManager 管理完整生命周期，包括 Slug 安全验证（防路径遍历）、3ms 快速恢复优化、四项创建后初始化（复制本地配置 / 配置 Hooks / 软链大依赖目录 / 复制忽略文件）、显式 cwd 模式（不 chdir 进程全局 cwd）、退出变更保护与自动/后台清理，并通过 isolation 字段与 SubAgent 自动绑定。

**大纲**
- 问题背景：分支仅时间隔离，切分支会刷新 mtime 引发全量重建
- Git Worktree：共享仓库、隔离文件，Git 2.5+ 特性
- WorktreeManager 设计：状态结构、fileCache 绑定
- Slug 安全验证：字符白名单 + 长度上限，防路径遍历攻击
- 创建流程：验证 → 锁检查 → 路径/分支名构建（worktree- 前缀、/ 转 +）→ 快速恢复（3ms 纯文件读取）→ git worktree add（GIT_TERMINAL_PROMPT、-B 覆盖）→ 记录持久化
- 创建后设置四项：本地配置 / Git Hooks / 软链大目录（含 Node __dirname 坑）/ 复制被忽略文件（.worktreeinclude）
- 进入与退出：显式 cwd 模式（session 持久化到 worktree_session.json）、退出变更保护（discardChanges 显式确认）、git worktree remove 与 branch -D 间的 sleep(100)
- 自动清理：无变更即删、有变更保留供主 Agent review
- 过期清理漏斗：临时命名模式（agent-a[hex] / wf_...）→ 过期时间 → fail-closed 变更与未推送 commit 检查
- 与 SubAgent 配合：isolation: worktree → executeWithWorktree 注入上下文通知 → autoCleanup → 结果带 Worktree 路径返回

---

## 工具生态

### MCP 协议与开放工具生态

- **文件**：[理论学习_MCP协议与开放工具生态.md](理论学习_MCP协议与开放工具生态.md)（约 38 KB）
- **架构定位**：④ 工具层扩展，让工具从「内置」变成「开放生态」。

**摘要**
以 USB 类比说明 MCP 将 Agent 与工具的 M×N 对接简化为 M+N。协议分 Data Layer（JSON-RPC 2.0 语义）与 Transport Layer（stdio / Streamable HTTP，旧 HTTP+SSE 已被取代）。完整会话分初始化握手、工具发现（tools/list）、工具调用（tools/call）三阶段，用请求 id 做异步响应匹配。通过 MCPToolWrapper 适配器将外部工具包装为内部 Tool 接口，支持项目级/用户级配置。工具延迟加载机制可降低约 85% token 开销、将工具选择准确率从 49% 提升至 74%，并需纳入权限系统管控。

**大纲**
- 背景：工具供给与 Agent 核心耦合，第三方无法独立扩工具
- USB 类比：M×N → M+N；Host（MewCode）/ Client / Server 三角色；Data Layer 与 Transport Layer
- 双方能力：Server 暴露 Tools / Resources / Prompts；Client 可声明 Roots / Sampling / Elicitation
- 传输层：stdio（子进程管道、stderr 走日志、stdout 不混非协议内容）vs Streamable HTTP（Accept: application/json, text/event-stream、认证）
- JSON-RPC 2.0：请求（有 id）/ 响应 / 通知（无 id）
- 会话三阶段：initialize 握手 → notifications/initialized → tools/list → tools/call×N
- 请求-响应异步匹配：pending map + 读取循环按 id 分发
- MCPToolWrapper 适配器：mcp_<server>_<tool> 命名防冲突
- 配置：项目级 .mewcode.yaml / 用户级 ~/.mewcode/config.yaml，项目级覆盖；${ENV} 注入密钥
- 启动时后台连接策略（否则 tools/list 死循环），部分失败不阻止启动
- 工具延迟加载：内置常驻、MCP 一律延迟；system-reminder 列名字 → ToolSearch 拉取（select: 精确 / 关键词搜索）→ 标记已发现
- 安全：纳入权限规则（mcp_github_*(*)）、可选命令白名单

---

## 交互层

### Slash Command 命令框架

- **文件**：[理论学习_Slash Command命令框架.md](理论学习_Slash Command命令框架.md)（约 21 KB）
- **架构定位**：交互层的命令框架，让常用操作绕过 Agent 引擎本地执行。

**摘要**
解决「清屏、查 token、切模式」这类无需 AI 参与的操作走 Agent Loop 白白消耗 Token 的问题。所有以 / 开头的输入被拦截本地处理，毫秒级响应。框架解决注册（声明式 + 读写锁）、解析（首个空格拆分命令名与参数）、执行（统一 CommandContext 依赖注入）三大问题。命令分 local / local-ui / prompt 三类，v1 内置 9 个命令。局限是仅支持硬编码预定义命令，自定义与 AI 结合能力交给 Skill 系统。

**大纲**
- 问题：博士按电灯开关（杀鸡焉用牛刀）；快车道绕过 Agent Loop
- 注册：Command 定义结构（name/aliases/description/usage/type/argPrompt/hidden/handler）；声明式注册集中管理；读写锁为 Skill 动态注册预留
- 解析：/ 开头 → 第一个空格拆分 → 小写化
- 执行：CommandContext（args/agent/conversation/session/UI/config）；UIController 接口隔离 UI 细节
- 三种类型：local（不走 Loop，/compact 内部调 LLM 也算）/ local-ui（改 UI 状态）/ prompt（构造 prompt 转交 Agent，/review）
- 别名：精确匹配 → 别名遍历；冲突启动时 panic
- 参数提示 argPrompt；Tab 补全（前缀匹配 + 下拉）
- 集成 UI 事件循环：拦截时机在发给 Agent 之前；错误提示永远带 /help 引导；状态栏模式提示
- 内置 9 命令：/help /compact /session /memory /status（local）；/clear /plan（local-ui）；/review（prompt）
- 边界：硬编码局限 → 下一章 Skill 系统，MCP prompts 也可包装为命令

---

### Skill 可复用技能包

- **文件**：[理论学习_Skill可复用技能包.md](理论学习_Skill可复用技能包.md)（约 20 KB）
- **架构定位**：交互层扩展，连接交互层与引擎层，把可复用 prompt 封装为一键触发技能。

**摘要**
解决三类痛点：重复解释同一上下文的效率损耗、MEWCODE.md 大杂烩无差别加载浪费 token、团队隐性经验难以沉淀。Skill 本质是「写给 Agent 的 SOP」，以 Markdown 定义（YAML frontmatter + prompt body），支持零代码调整。与 Slash Command 明确分工：确定性操作走 Command，需 AI 判断且流程可复用的走 Skill。支持 inline（共享上下文）/ fork（独立上下文）两种执行模式、三级搜索优先级、两阶段加载（轻量元信息 → 按需 LoadSkill），并位于 Function Calling + MCP 之上的编排层。

**大纲**
- 三类痛点：重复解释 / 大文件无差别加载 / 知识流失
- Skill = 写给 Agent 的 SOP，指令需比人类 SOP 更精确；Anthropic Agent Skills 开放标准
- 与 Slash Command 分工：Agent 主动发现、可携带整套资源、inline/fork 模式；最新 Claude Code 已合并
- Markdown 定义格式：YAML frontmatter（name/description/model/mode/context）+ prompt body
- 三级搜索优先级：项目级 > 用户级 > 内置级，同名高优先级覆盖
- inline vs fork：/commit 用 inline（需看修改上下文），/review 用 fork（客观性）；fork 支持 context: full/recent/none
- 自动注册为 Slash Command（/help 可见、Tab 补全）
- 意图识别：两阶段加载（轻量注册 → LoadSkill 按需加载）；目录型 Skill（SKILL.md + examples/ + scripts/）
- 完整流程：扫描 frontmatter → 注入消息 → 注册命令 → 意图识别 → LoadSkill → 执行
- 生态定位：Function Calling 负责调用、MCP 负责接入、Skill 负责编排

---

### Hook 生命周期钩子

- **文件**：[理论学习_Hook生命周期钩子.md](理论学习_Hook生命周期钩子.md)（约 26 KB）
- **架构定位**：③ 工具层的 Hook 系统，工具层与安全层的桥梁。

**摘要**
让用户在 Agent 生命周期事件上挂载自动化动作，省去「人肉 CI」。Hook 由事件、条件、动作三要素组成，支持会话级 / 轮次级 / 工具级 / 消息级 / 系统级十余种事件。pre_tool_use 是唯一可「拦截」的事件（reject: true 取消工具调用并形成反馈循环）。条件语法复用权限规则（== != =~ ~= + && ||），四种动作执行器（command / prompt / http / agent），另有 once、async 与错误兜底等执行控制。

**大纲**
- 痛点：反复手动格式化、代码生成、拦截危险命令、注入项目上下文
- 三要素：事件 / 条件 / 动作；配置在 .mewcode/config.yaml + 用户级 + config.local.yaml，追加合并
- 事件分类：session_start/end、turn_start/end、pre/post_tool_use、pre_send/post_receive、startup/shutdown/error/compact、permission_request/file_change/command_execute
- pre_tool_use 拦截能力：reject → ToolRejectedError → 反馈循环；YAML 顺序决定执行顺序，reject 后终止后续
- 条件语法：四操作符、&& || 组合（不可混用，避免优先级解析器）、glob 助记（=~ 正则 / ~= glob）
- 四种执行器：command（shell + 变量替换 + timeout）/ prompt（system-reminder 注入，不影响缓存）/ http（外部通知）/ agent（AI 监督 AI，接口留到 SubAgent 章）
- 执行控制：once（会话级，重启重置）、async（pre_tool_use 禁用）、错误只记日志不中断主流程
- 上下文变量：HookContext 字段与 $VAR 替换规则，未定义变量替换为空串
- 与 Agent Loop 集成：runHooks / runPreToolHooks 插入点
- 实战配置五例：自动格式化、禁改 vendor、项目上下文、拦截 rm -rf、Slack 通知
- 配置加载与校验：事件白名单、action 类型、reject/async 约束、必填字段

---

## 引擎扩展 · Agent 协作

### SubAgent 子任务分发

- **文件**：[理论学习_SubAgent子任务分发.md](理论学习_SubAgent子任务分发.md)（约 42 KB）
- **架构定位**：引擎层扩展，把 Agent 包装成工具，实现引擎层与工具层递归组合。

**摘要**
解决单 Agent 多任务导致的上下文污染问题。关键洞察是 Agent 与 Tool 接口同构，可将 Agent 包装为统一 Agent 工具，通过 subagent_type 参数选择类型。两种创建模式：定义式（固定角色、能力边界、前台同步）与 Fork 式（继承父对话历史、强制后台异步、可命中 prompt cache）。上下文隔离按「运行时状态隔离、基础设施共享」划分，工具过滤有四层防线，内置 Explore / Plan / general-purpose / Verification 四类 Agent。

**大纲**
- 问题：上下文污染（重构中间信息干扰写测试）、双任务互相干扰
- 关键洞察：Agent 与 Tool 接口同构 → Agent 工具，统一通过 subagent_type 选类型
- 定义式 vs Fork 式：能力边界 vs 继承上下文；Fork 命中 prompt cache 降本；Fork 强制后台、不可再 Fork（QuerySource 检测 + FORK_BOILERPLATE_TAG 兜底）；Fork Boilerplate 规则块
- 上下文隔离：文件缓存、权限追踪、Token 用量隔离；LLM 客户端、工具集、Hook 引擎、文件系统共享
- Agent 定义即 Markdown：YAML frontmatter + body（与 Skill 同构但语义不同）；model / permissionMode: dontAsk；四来源优先级（项目 > 用户 > 内置 > 插件）
- RunToCompletion：非交互式执行，任务直接注入，无工具调用即返回最后文本
- 父子链路与嵌套限制：Fork 不能再 Fork、后台 Agent 不能 spawn Agent
- 后台运行模式：run_in_background / 120 秒超时自动切换 / Esc 手动切换 / Fork 无条件后台；BackgroundTask + TaskManager + <task-notification>；adoptRunning 前后台移交；后台工具白名单 ASYNC_AGENT_ALLOWED_TOOLS
- 工具过滤四层：全局禁止列表 → 自定义额外禁止 → 后台白名单 → Agent 定义的 tools/disallowedTools
- 内置 Agent：Explore（只读黑名单 + haiku）、Plan（只读规划，与 Plan 权限模式区分）、general-purpose（全能力）、Verification（enableVerificationAgent 开关，找最后 20% bug）

---

## 记忆层（跨会话）

### 跨会话记忆与会话持久化

- **文件**：[理论学习_跨会话记忆与会话持久化.md](理论学习_跨会话记忆与会话持久化.md)（约 38 KB）
- **架构定位**：④ 记忆层，焦点从会话内压缩转向跨会话持久记忆。

**摘要**
对应人类记忆逻辑构建分层记忆：工作记忆（200K 上下文窗口）+ 长期记忆（会话持久化 / 项目指令文件 / 自动记忆）。MEWCODE.md 是 Agent 的「入职文档」，支持 4 层优先级（高优先级靠后排列）、@ 引用模块化与递归/越界防护。会话持久化选 JSONL 格式（O(1) 追加、崩溃安全、增量加载），恢复时处理消息链修复、Token 检查、时间跨度提示。自动记忆分 user / feedback / project / reference 四类，独立 .md 文件 + MEMORY.md 索引，后台定期记忆治理。

**大纲**
- 问题：跨会话失忆、个人偏好反复重述
- 记忆分层：工作记忆（上下文窗口）vs 长期记忆三种形态
- 项目指令文件 MEWCODE.md：与 README 的区别；4 层优先级拼接（高优先级靠后）；两个项目级位置（git 提交 vs 忽略）；@ 引用递归（depth ≤ 5 + visited 防环路 + 路径越界拦截）
- 会话持久化：JSONL 选择理由（对比 SQLite / 普通 JSON）；O(1) 追加 / 崩溃安全 / 增量加载；SessionRecord 结构；与协议无关的内部表示（换厂商不失效）；文件命名与组织；SessionManager；恢复四步（逐行解析 → 修复工具调用链补位 → Token 检查 → 时间跨度提示）；30 天过期清理
- 自动记忆：四类分类存储（user/feedback → 用户级，project/reference → 项目级）；独立 .md 文件 + MEMORY.md 索引（200 行 / 25KB 上限 ≈ 1-2% 窗口）；提取时机（每轮 Loop 完成后异步）；去重交给 LLM 判断
- 记忆治理（autoDream）：门控条件（目录存在 / 24h / 10min 节流 / 会话数 ≥ 5 / 锁文件）；锁文件 PID + mtime 双用途；四阶段整理 prompt；提取管「写」、治理管「整理」

---

## 验收评测

### 给你的 Agent 跑一次真实评测

- **文件**：[给你的Agent跑一次真实评测.md](给你的Agent跑一次真实评测.md)（约 26 KB）
- **性质**：MewCode 课程系列收尾实操篇，非架构设计类文档。

**摘要**
讲解基于 SWE-bench-Live（微软维护、每月收录最新真实 GitHub issue）搭建 Code Agent 评测流水线。给出完整操作步骤：环境准备（Docker / Python 3.10+ / 16GB 内存，国内需镜像加速）、封 git 历史防作弊、gold 补丁校验环境、Docker 判分。强调「通过率上限由模型能力决定、框架缺陷只会拉低可解题得分」，需翻运行记录归类失败原因（定位错误 / 修法不对 / 碰坏测试 / 越改越大 / 中途退出）。对照测试必须钉死模型变量，重点比轮次、token、耗时等框架效率指标。并提供面试表述要点与用公司 PR 出题的进阶方案。

**大纲**
- 评测在测什么：自动化测试把「好不好用」量化；SWE-bench-Live + 官方 harness
- 动手跑一遍：环境准备（系统 / Docker / 内存 / 磁盘 / 镜像加速）；把流水线整包交给 Claude Code 执行的提示词模板
- 关键防作弊：git archive 封历史只留单 commit；排除运行时目录与测试文件再导出补丁
- 前置校验：gold 补丁先跑一遍，环境失效的题剔除
- 结果判断：FAIL_TO_PASS 全过 + PASS_TO_PASS 无损坏才算过；5-10 道解 1-3 道属正常；警惕 gold 不过与空补丁
- 看失败：翻 jsonl 记录归类失败原因；案例（Agent 卡在「不要改测试」约束主动放弃正确方向）
- 改完再跑：一次只动一个变量；盯具体现象而非通过率；案例（修好参数校验但分没涨，因通过率上限在模型）
- 怎么判断 Agent 行不行：拆开三件事——质量（钉死模型比通过率）/ 效率（轮次 token 耗时）/ 框架自身毛病（只有翻记录可见）
- 对照测试：Claude Code 接同一模型，封历史防抄袭，比效率指标
- 进阶：用公司 PR 出题（挑 PR → 改写任务删「怎么解决」→ 抽测试作判分 → 封 git 历史）
- 面试表述：测试怎么做 / 效果怎么样 / 效率与 token；信息保留测试为另一种测法

---

*本索引由 Claude 通读全文提炼生成，供快速检索与复习使用；详细内容请以原文为准。*
