# Coding Agent 技术规格 · 10 SubAgent 与 Worktree

> 状态：**已确认 v0.2**（2026-08-13，T25-T27 已决）｜里程碑：阶段 5（生产级：可隔离并行）｜依赖：`02`（Agent Loop 扩展 RunToCompletion）、`03`（ToolRegistry 注册 Agent/Task 工具）、`06`（子 Agent 权限模式）、`05`（/tasks 命令）｜被依赖：`09`（Skill fork 接线，T22）、`08`（Dreaming 受限代理增强）、`11`（评估隔离跑批）

---

## 1. 目标与范围

**本模块回答：单个 Agent 上下文被污染了怎么办？多个 Agent 并行工作怎么不撞车？**

- **SubAgent**：把 Agent 包装成工具，主从分发——主 Agent 把子任务交给独立上下文的子 Agent，做完成果回报，主上下文不被中间过程污染。解决「上下文污染」「Token 飙升」「双任务互相干扰」。
- **Worktree**：Git Worktree 做**文件系统级**隔离——子 Agent 在自己独立的工作目录工作，彻底消除并行改文件的冲突（分支只提供时间隔离，切分支刷新 mtime 引发全量重建）。

**核心公式**：`Agent ≈ Tool`（有名字、有描述、收参数、返结果的可执行单元）——Agent 与 Tool 接口同构，把 Agent 注册成工具，引擎层与工具层递归组合。

**不在本模块范围**：
- Skill 的 SOP 定义与加载 → `09`（本模块只提供 fork 执行的底层基建）
- 权限裁决本体 → `06`（本模块复用其模式矩阵 + 子 Agent 专用 `dontAsk`）
- 多 Agent 协作框架（CrewAI/AutoGen 式平等群聊）——本模块是**主从分发**：主 Agent 唯一调度者，子 Agent 做完成果回报，coding 场景更稳定（主 Agent 始终掌握全局，不陷入互相等待循环）
- 评估跑批编排 → `11`（`10` 的隔离跑批是 11 的可用手段）

---

## 2. 与相邻模块关系

```
   02 ReAct Loop（主） ──调用 Agent 工具──► 10 SubAgent
        │  run_to_completion（新增）              │ 定义式（空白对话）/ Fork 式（继承历史）
        ▼                                        ▼
   03 ToolRegistry ◄── 注册 Agent / TaskList / TaskGet / TaskCreate / TaskUpdate 工具
        │                                        │ isolation: worktree
   06 权限规则 ◄── 子 Agent permissionMode（default/acceptEdits/dontAsk）──► 10 WorktreeManager
        │                                        │
   07 trace ◄── sub_agent.run span / 独立 token 统计 ◄─┘ git worktree 生命周期
        │
   09 Skill fork ──复用 SubAgent 底座──► 10（T22 接线）
   08 Dreaming ──可复用受限代理──► 10（增强，不改 08 已决设计）
```

- **消费 `02`**：Agent Loop 新增非交互式 `run_to_completion`（任务注入 → 无工具调用即返回最后文本），与主 Loop 共用四类停止条件与事件流。
- **消费 `03`**：注册 `Agent` 工具（subagent_type 参数选类型）+ 4 个 Task 工具；子 Agent 工具过滤复用 `ToolRegistry` 过滤能力。
- **消费 `06`**：子 Agent 权限走 `06` 模式矩阵，新增 `dontAsk`（自动批准，依赖能力边界锁死安全）。
- **为 `07`**：子 Agent 的 span 挂在父 trace 下（`sub_agent.run`），独立 token 用量统计（07 按 session 聚合的补充维度）。
- **为 `09`**：Skill fork = 创建独立 Conversation/Agent Loop/工具过滤集，`10` 落地后 Skill fork 从「降级 inline」切换到复用本模块基建（T22）。
- **为 `08`**：Dreaming 治理的「后台受限代理」可升级为定义式 SubAgent（工具集受限），08 已确认设计不改。

---

## 3. 详细设计

### 3.1 核心洞察：Agent 也是一种工具

```
AgentTool implements Tool:
    name = "Agent"
    parameters = {
        prompt:           {type: string, required: true},   # 任务描述
        description:      {type: string, required: true},   # 给模型的决策信息
        subagent_type:    {type: string, optional: true},   # 预定义类型；留空 = Fork
        model:            {type: string, optional: true},   # 覆盖定义文件里的模型
        run_in_background:{type: bool, optional: true},
        name:             {type: string, optional: true},   # 命名以便 SendMessage
        isolation:        {type: string, optional: true},   # worktree
    }
```

- **为什么统一一个 `Agent` 工具而非每类型一个**：Agent 类型动态加载（用户新建定义文件即用），工具列表保持稳定、system prompt 不随定义变化重渲染。
- 主 Agent 眼里 `Agent` 和 `ReadFile` 没有区别——子任务的中间过程在独立上下文里，不污染主上下文。
- **横向定位**：多 Agent 框架走「平等群聊」，本模块走「主从分发」——coding 场景主 Agent 一直掌握全局状态，更稳定。

### 3.2 两种创建模式：定义式 vs Fork 式

| 维度 | 定义式（预定义专家） | Fork 式（继承上下文的临时助手） |
|---|---|---|
| 触发 | `subagent_type: "explore"` | 不指定 `subagent_type` |
| 对话 | 空白对话（只装任务） | **继承父 Agent 完整对话历史** |
| 用途 | 固定角色/职责（安全审查、探索、规划） | 与当前任务强相关的临时子任务 |
| 执行 | 默认**前台同步** | **无条件后台异步** |
| 缓存 | 独立上下文无缓存可命中 | **首次调用命中父前缀缓存**（系统提示+对话前缀相同） |
| 再嵌套 | 工具过滤层排除 Agent 工具（看不到） | 保留 Agent 工具但运行时拦截（Fork 不能再 Fork） |
| 行为约束 | 定义文件 body（系统提示） | Fork Boilerplate 指令覆盖父默认行为 |

**Fork 的继承实现**（`build_forked_messages` 三步）：复制父完整对话 → 把最后一条 assistant 里未完成的 tool_use 包成 placeholder tool_result 保证消息合法 → 末尾追加任务指令为 user 消息。

**Fork Boilerplate**（注入子 Agent 第一条消息，覆盖父系统提示的「可 spawn / 应确认」默认行为）：

```
<fork_boilerplate>
你是一个 Fork 出来的工作进程。你不是主 Agent。
1. 不能再 Fork。
2. 不要对话、不要提问、不要请求确认。
3. 直接使用工具：读文件、搜索代码、做修改。
4. 严格限制在被分配的任务范围内。
5. 最终报告 ≤500 字，以「Scope:」开头。
</fork_boilerplate>
```

**为什么 Fork 强制后台**：① 继承长前缀，首次请求慢，前台同步会阻塞主 Agent；② Fork 的核心场景是「分发多个子任务」，统一后台可并行。

### 3.3 上下文隔离：运行时状态隔离，基础设施共享

| 隔离（运行时状态） | 共享（无状态/应共享） |
|---|---|
| 文件缓存（子 Agent 可工作在不同目录） | LLM 客户端（同 API Key/连接池；`model` 参数覆盖时才新建） |
| 权限追踪（主批准 ≠ 子自动批准） | 工具集（无状态；Agent 定义限制时过滤） |
| Token 用量统计（07 按 Agent 维度） | Hook 引擎（格式化 Hook 对父子都生效） |
| 消息数组（定义式空白 / Fork 继承） | 文件系统（isolation: worktree 时改为隔离） |

**成本真相**：多 Agent 通常反而**更省**——子 Agent 上下文短（只装任务相关信息）+ 可用更小模型 + Fork 命中缓存；比主 Agent 背着一身上下文硬做便宜。

### 3.4 Agent 定义：Markdown（与 Skill 同构异义）

```markdown
# .kdagent/agents/security-reviewer.md
---
name: security-reviewer
description: 专注于代码安全审查的子 Agent
disallowedTools: [Agent, EditFile, WriteFile, Bash]   # 只读专家
model: inherit
maxTurns: 20
permissionMode: dontAsk          # 工具调用自动批准（能力边界已锁死）
---

你是一个专注于代码安全审查的 Agent。
## 职责 / ## 规则 ...
```

- **与 Skill 的异同**：结构都是 YAML frontmatter + Markdown body（解析逻辑复用）；**语义不同**——Skill body 是注入对话的 SOP 指令（经 LoadSkill 进对话历史）；Agent body 是子 Agent 启动时的**系统提示**（决定这个新 Agent 是谁、能做什么），伴随整个生命周期。
- **frontmatter 核心字段**：`name`(=agentType)、`description`(=whenToUse)、`tools`/`disallowedTools`（白名单确定范围、黑名单排除——**优先黑名单**：角色能力接近全集，排除少数危险工具更方便，新只读工具自动可用）、`model`、`maxTurns`、`permissionMode`、`isolation`、`background`、扩展字段 `skills`/`mcpServers`/`hooks`。
- **来源优先级**（同名高优先级覆盖）：项目级 `{work_dir}/.kdagent/agents/` > 用户级 `~/.kdagent/agents/` > 内置级 > 插件级。

### 3.5 RunToCompletion：非交互式执行

子 Agent 无用户等待，任务直接注入、跑完返回最后文本——与主 Loop 几乎一致，仅两点差异：

```
def run_to_completion(agent, task) -> str:
    agent.conversation.add_user_message(task)      # 任务注入，不等用户输入
    last_text = ""
    for _ in range(agent.config.max_turns):
        blocks, stop = await agent._llm_call()
        agent.conversation.add_assistant_message(blocks)
        if has_tool_use(blocks):
            last_text = extract_text(blocks)
            results = await agent._execute(blocks)   # 工具过滤四层已定工具集
            agent.conversation.add_tool_results(results)
        else:
            return extract_text(blocks) or last_text   # 纯文本 → 完成
    return last_text
```

- Hook 在子 Agent 中**仍然生效**（pre/post_tool_use 插入点复用）。
- 权限由 `permissionMode` 决定：`dontAsk` + `disallowedTools` 锁死能力边界 → 全自动无弹窗。

### 3.6 工具过滤四层防线

```
第 1 层：全局禁止 ALL_AGENT_DISALLOWED_TOOLS
         → 所有子 Agent 不能用：Agent（防递归）、AskUserQuestion（防阻塞）、TaskStop …
第 2 层：自定义 Agent 额外禁止
         → 定义式走工具过滤；Fork 继承全部工具不过滤，防递归靠运行时拦截
第 3 层：后台白名单 ASYNC_AGENT_ALLOWED_TOOLS
         → 后台 Agent 只能用基础读写/搜索/Bash/网络，不含 Agent/Task*（防后台嵌套失控）
第 4 层：Agent 定义 tools + disallowedTools
         → 白名单定范围、黑名单从中排除
```

- **Fork 防递归两道闸**：`QuerySource` 运行时检测（caller 是 Fork 路径 → 报错）+ `FORK_BOILERPLATE_TAG` 扫描兜底（对话压缩弄丢信号时）。
- 四层依次过滤，最终得到子 Agent 实际工具集。

### 3.7 后台运行模式

**四种进入路径**：① 调用传 `run_in_background: true`；② 前台超 120s 自动切换（`get_auto_background_ms`）；③ 用户 Esc 手动切换；④ Fork 无条件后台。

```
BackgroundTask:
    id, sub_agent, task
    status: running | completed | failed
    result: str
    start_time / end_time
    cancel: Callable
    progress: ProgressTracker      # 工具次数、token、最近活动
```

- **TaskManager**：`launch` 在后台协程跑 `run_to_completion`，完成推 `notifyChannel` → 主 Agent 消息循环向对话注入 `<task-notification>`（不打断当前对话）。
- **adoptRunning**：前台→后台切换的桥梁——把运行中 Agent 实例/事件流/取消函数/部分结果移交 TaskManager 继续消费，不杀掉重来。
- **Task 工具**（4 个内置）：`TaskList` / `TaskGet` / `TaskCreate`（给 Hook 用）/ `TaskUpdate`。**不给后台任务做 slash command 栈**：用户问「后台任务咋样了」→ 主 Agent 自己用 TaskList/TaskGet 查 → 自然语言回答。`/tasks`（05 补注册，local）仅作列表便捷入口（T26 已决）。

### 3.8 内置 Agent 类型（T25/T27 已决）

| 类型 | 能力 | 模型 | 要点 |
|---|---|---|---|
| `Explore` | 只读搜索（Glob/Grep/Read，Bash 仅只读命令） | cheap 档（可配） | 黑名单 `[EditFile, WriteFile]`——新只读工具自动可用；项目结构探索、调用链梳理 |
| `Plan` | 只读规划，输出分步计划 + 3-5 个关键文件路径 | 主模型 | **与 Plan 权限模式区分**：Plan 模式是主 Agent 自身切只读状态（05 /plan）；Plan Agent 是独立上下文的规划子 Agent，规划过程完全隔离 |
| `general-purpose` | 全部工具 | inherit | 需要完整能力但独立上下文的场景 |
| `Verification` | 找最后 20% bug（跑构建/测试/lint，VERDICT: PASS/FAIL） | inherit | `enable_verification_agent: true` 配置开关启用（**默认关**） |

### 3.9 与 08 / 09 的接线

- **Skill fork（09 T22）**：`10` 落地后，Skill fork 从「降级 inline」切到复用本模块——SkillForkHost 委托创建独立 Conversation/Agent Loop/工具过滤集（Fork 式），参数走 SKILL.md frontmatter（mode/context），不暴露给模型选；嵌套限制（Fork 不能 Fork、后台不能 spawn）对 Skill fork 同样生效。
- **08 Dreaming**：治理的「后台 fork 受限代理」可升级为定义式 SubAgent（工具集 = 文件工具 + Grep，`dontAsk` + 只读），08 已确认设计不变，仅实现路径更稳。

### 3.10 Git Worktree：空间隔离

**为什么分支不够**：分支是**时间隔离**（不同时间点的快照），同一时刻只有一个工作目录；切分支刷新 mtime 引发增量构建退化为全量重建。Worktree 是**空间隔离**——同一仓库多个独立工作目录、共享 `.git`、版本历史统一。

```
./project/            → main 分支（主 Agent）
./project/.kdagent/worktrees/agent-3f2b1c0/   → worktree-agent-3f2b1c0 分支（子 Agent）
```

### 3.11 WorktreeManager 生命周期

```
WorktreeManager:
    repo_root / worktree_dir(仓库内 .kdagent/worktrees/) / lock
    active: dict[name, Worktree]   # name/path/branch/based_on/head_commit/created
    current_session: WorktreeSession | None   # 持久化 .kdagent/worktree_session.json（--resume 基础）
    file_cache: FileCache   # 进出时清理，保证缓存一致
```

- **Slug 安全验证**（防路径遍历，LLM 输入不可信）：白名单 `[a-zA-Z0-9.-_]` + 长度 ≤64；`/` 作嵌套分隔符分段校验（`team-refactor/alice`）。
- **创建六步**：验证 → 锁内查重 → 构建路径/分支名（`worktree-` 前缀、`/`→`+`）→ **快速恢复**（不调 git 子进程，纯读文件系统还原 head SHA，~3ms；`git worktree add` 大仓库要数秒）→ `git worktree add -B`（`GIT_TERMINAL_PROMPT=0` + `GIT_ASKPASS=""` + stdin ignore 三重防挂起；`-B` 覆盖孤儿分支）→ 记录状态持久化。
- **创建后四项设置**（只对新建）：A 复制本地配置（config.local.yaml 等不入库文件）；B 配置 Git Hooks（`core.hooksPath` 不自动继承，优先 `.husky/` 回退 `.git/hooks/`）；C 软链大依赖目录（node_modules/.venv，`symlink_directories` 配置化——best-effort，Node `__dirname` 会解析到真实路径的坑需文档标注）；D 复制被忽略但需要的文件（`.worktreeinclude` 用 gitignore 语法声明，.env 最典型）。
- **进入：explicit cwd 模式**——**不 chdir 进程全局 cwd**（cwd 是全局可变状态，并发组件共享会成同步点）；工具从 session 显式取 worktree 路径作本次调用 cwd。文件缓存 key 用绝对路径，主目录与 worktree 版本天然不冲突，无需清缓存。
- **退出**：变更保护（`action=remove` 且未 `discard` 时有 uncommitted/newCommits → 拒绝，防 LLM 误删工作成果）→ chdir 回原 cwd 兜底 → 清 session + 持久化 null（否则 `--resume` 会尝试恢复已删除目录）→ 可选 remove（`git worktree remove --force` + sleep(100) 等 lockfile 释放 + `git branch -D`，生产环境用指数退避重试）。
- **自动清理**（子 Agent 场景）：无变更（`git status --porcelain` 空 + 无新 commit）→ 删；有变更 → 保留 + 路径/分支追加进返回结果供主 Agent review。
- **过期清理漏斗**（孤儿安全网）：临时命名模式过滤（`agent-a[hex]` / `wf_...`，用户 `/worktree create my-feature` 不匹配永不清）→ 过期时间 → **fail-closed** 变更检查（有 uncommitted 或未推送 commit 不删，宁可多占磁盘不丢成果）。

### 3.12 与 SubAgent 配合：isolation: worktree

Agent 定义 `isolation: worktree` → 自动绑定：

```
execute_with_worktree(def, task):
    wt_name = "agent-" + gen_id()[:8]
    wt = worktree_manager.create(wt_name, "HEAD")
    task = worktree_notice + "\n\n" + task    # 注入上下文通知
    sub = create_from_definition(def); sub.set_workdir(wt.path)
    result = sub.run_to_completion(task)
    kept = auto_cleanup(wt)                    # 有变更保留
    return result + ("[Worktree 保留于 …，分支 …]" if kept else "")
```

**上下文通知**告诉子 Agent 三件事：继承了父对话；当前在独立 Worktree；父传路径指向主目录、需翻译成本地路径并重新读文件——否则它读到 worktree 文件却按主目录版本来理解，产生认知偏差。

### 3.13 Windows 平台约束

- `git worktree` 是跨平台命令（Git for Windows 2.5+ 原生支持），核心流程无平台差异。
- **软链大目录**：Windows 符号链接需管理员权限或开启开发者模式 → `symlink_directories` 为 best-effort，失败仅警告不中断（Python 可用 `os.symlink`，权限不足时捕获）。
- **explicit cwd 模式天然兼容 Windows 盘符路径**（工具显式传绝对路径，无 chdir 全局状态依赖）。
- 所有 git 子进程统一设 `GIT_TERMINAL_PROMPT=0`（Windows 无交互终端，防挂起）。

### 3.14 关键参数

| 参数 | 初值 | 含义 |
|---|---|---|
| `Agent` 工具 | 统一一个，`subagent_type` 选型 | 工具列表稳定 |
| 子 Agent 模型 | 定义文件 `model`；默认继承主模型 | 独立上下文，换模型不破坏主缓存 |
| 子 Agent `maxTurns` | 20（定义文件可调） | 防失控循环 |
| 后台自动切换 | 120s | 前台超时转后台 |
| Fork 报告上限 | ≤500 字，`Scope:` 开头 | 父解析结构化结果 |
| Worktree 目录 | 仓库内 `.kdagent/worktrees/` | 已 .gitignore |
| 过期清理 | 临时命名 + 过期时间 + fail-closed | 孤儿安全网 |
| Task 工具 | TaskList/Get/Create/Update | 后台任务管理 |

---

## 4. 参考实现

| 内容 | 参考 |
|---|---|
| Agent≈Tool、两种创建模式、上下文隔离、Agent 定义 Markdown、RunToCompletion、嵌套限制、后台运行、工具过滤四层、内置 Agent | [SubAgent 子任务分发](../mewcode设计文档/理论学习_SubAgent子任务分发.md) |
| Worktree 空间隔离、WorktreeManager 全生命周期、四项设置、explicit cwd、变更保护、自动/过期清理、与 SubAgent 配合 | [Git Worktree 并行隔离](../mewcode设计文档/理论学习_Git%20Worktree并行隔离.md) |
| 子 Agent 权限模式与 permissionMode | `06`（本模块新增 `dontAsk` 子上下文语义） |
| Task 工具 / /tasks 命令挂载 | `05` §3.5（CommandRegistry 动态注册） |
| Skill fork 接线 | `09` §3.10（T22） |
| 子 Agent token 统计 / trace 关联 | `07` §3.5/§3.1 |

---

## 5. 验收标准

- [ ] 主 Agent 可调用 `Agent` 工具委派任务；结果正确回传，主上下文无子任务中间过程污染
- [ ] 定义式：`subagent_type=explore` 空白对话、只读探索、返回结果；`permissionMode: dontAsk` 无弹窗自动执行
- [ ] Fork 式：继承父对话历史；首次调用命中前缀缓存；Fork 内再 Fork 被拦截（QuerySource/标记兜底）
- [ ] RunToCompletion：任务注入执行完返回最后文本；Hook 在子 Agent 中生效
- [ ] 工具过滤四层生效：子 Agent 调不到 `Agent`/`AskUserQuestion`；后台 Agent 只能基础工具；`disallowedTools` 锁死能力
- [ ] 后台：`run_in_background` / 120s 超时 / Esc 切换 / Fork 无条件后台四条路径都进 TaskManager；完成注入 `<task-notification>`
- [ ] `adoptRunning` 前台切后台：实例/事件流/部分结果无损移交
- [ ] `TaskList`/`TaskGet` 查询后台任务状态与结果；`/tasks` 命令可见列表
- [ ] 内置 4 类 Agent 可用；`enable_verification_agent` 开关控制 Verification 是否出现
- [ ] `isolation: worktree`：子 Agent 在独立目录工作，改动不碰主目录；无变更自动清理、有变更保留供 review
- [ ] Worktree 过期清理：孤儿目录被清，有 uncommitted/未推送 commit 的保留
- [ ] Slug 注入（`../../etc`）被拒；退出时未确认 discard 不删有变更的 Worktree
- [ ] 子 Agent span 挂父 trace、token 独立统计（07 可消费）

---

## 6. TO-DECIDE

| # | 待决项 | 关联 |
|---|---|---|

> ✅ 已决（2026-08-13）：**T25** → 内置 Agent **4 类全内置**（Explore/Plan/general-purpose/Verification）；Explore 用便宜档模型（主 provider DeepSeek，独立上下文换模型不破坏主缓存），其余默认继承主模型，`model` 字段可覆盖。**T26** → 后台任务交互 = **Task 工具为主 + `/tasks` 便捷列表**（local 仅查看），不造管理栈。**T27** → Verification **默认关**（`enable_verification_agent: true` 开启）。
