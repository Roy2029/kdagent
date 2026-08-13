# Coding Agent 技术规格 · 06 权限与 Hook

> 状态：**已确认 v0.2**（2026-08-12）｜里程碑：阶段 3（可用+可控档）｜依赖：`02`（_exec_one 嵌入点/AgentEvent）、`03`（工具元信息/require_confirm 接管）、`05`（ConfirmDialog/权限模式状态栏）｜被依赖：`07`（permission/hook 决策可观测）、`09`/`10`（外部工具与子代理复用裁决）

---

## 1. 目标与范围

**本模块是 Agent 的「安全刹车」与「自动化抓手」**。它回答两个问题：怎么确保 Agent 不做危险的事（权限），怎么在生命周期关键节点自动挂动作（Hook）。

- **权限**：应用层五层纵深防御——黑名单 / 路径沙箱 / 权限规则 / 权限模式 / HITL。安全姿态默认收紧，拒绝不终止 Loop，用户可「始终允许」逐步放权。
- **Hook**：事件驱动自动化——事件 + 条件 + 动作，`pre_tool_use` 是唯一能「说不」的事件。
- **与 03 的关系**：`03` 预留的 `require_confirm` 极简确认在此**升级为完整裁决系统**；`ToolContext` 的 permission 依赖位在此注入。

**不在本模块范围**：
- **OS 级沙箱**（seatbelt / bubblewrap + seccomp）——参考实现为 macOS/Linux 内核方案，本平台为 Windows，无可直接套用的轻量等价物。**可控档不做 OS 层隔离，仅应用层五层防线**（T17 待决，生产级再评估）。
- 网络隔离、sandbox 模式、autoAllow 联动——依附 OS 沙箱，随之顺延。
- MCP 外部工具 / Skill 的权限适配 → `09`（本模块定义通用裁决函数，wrapper 复用）。
- SubAgent 的权限继承与隔离 → `10`。

---

## 2. 与相邻模块关系

```
        02 ReAct Loop（_exec_one）
           │ validate_input
           ▼
   ┌─────────────── 06 权限与 Hook ───────────────┐
   │  ① 权限五层裁决（消费 03 元信息）             │
   │     DENY ──► is_error 结果（Loop 继续）       │
   │     ASK  ──► 05 ConfirmDialog（HITL）         │
   │  ② pre_tool_use Hooks（可拦截 reject）        │
   │  ③ execute（03）→ ToolResult                  │
   │  ④ post_tool_use Hooks                        │
   └──────┬──────────────────────────┬───────────┘
          │ 决策记录（allow/deny/ask）│ Hook 执行记录
          ▼                          ▼
       07 可观测性              07 Trace（span）
```

- **消费 `03`**：`is_read_only` / `is_destructive` / `category` 是模式矩阵与规则的判定依据；`require_confirm` 字段被接管（工具层不再自实现裁决）；`ToolContext.permission` 依赖位注入裁决器。
- **消费 `05`**：`ASK` 决策 → `05` ModalScreen 确认对话框（y 允许 / n 拒绝 / a 始终允许）；状态栏展示当前权限模式；`/plan` 绑定 plan 模式。
- **消费 `02`**：嵌入 `_exec_one`（validate 后、execute 前）；HITL 阻塞经 AgentEvent 流新增 `PermissionRequestEvent`（`02` 事件枚举扩展）。
- **为 `07` 提供**：每次裁决产生一条决策记录（工具/内容摘要/效果/耗时）→ Trace span；Hook 执行记录同理。
- **为 `09`/`10` 提供**：`PermissionChecker.check` 是无 UI 依赖的纯函数，MCP wrapper 与 SubAgent 复用同一裁决链。

---

## 3. 详细设计

### 3.1 总体架构：五层防线 + 嵌入 Loop

```
用户输入
  ↓
[L1] 危险命令黑名单      —— 硬拦截，最高优先级（仅 Bash）
  ↓
[L2] 路径沙箱            —— 越界文件操作 → ASK（仅 filesystem 类）
  ↓
[L3] 权限规则            —— ToolName(pattern)，deny > ask > allow
  ↓
[L4] 权限模式            —— 整体策略（default/acceptEdits/plan/bypassPermissions）
  ↓
[L5] HITL 确认           —— 人在回路，兜底（05 ModalScreen）
  ↓
      pre_tool_use Hooks —— 动态细粒度拦截（reject）
  ↓
      工具执行 → post_tool_use Hooks
```

**层间原则**：上一层能决策直接返回，不能决策才下传；L3 无规则命中（UNKNOWN）才落 L4；L4 返回 ASK 才触发 L5。**顺序决策：权限基线整体先于 Hook**——黑名单与静态规则是安全底线，任何 Hook 都不能绕过；Hook 在基线上做参数内容的动态拦截（理由详见 §3.10）。

### 3.2 裁决核心：PermissionChecker

```python
# permission/checker.py
@dataclass
class Decision:
    effect: Literal["allow", "deny", "ask"]
    reason: str                   # 命中哪一层/哪条规则
    rule: str | None = None       # 命中的规则串，如 Bash(git push --force*)

class PermissionChecker:
    def __init__(self, mode_provider, rule_engine, sandbox): ...
    def check(self, tool: Tool, input: dict) -> Decision: ...
    def learn(self, tool_name: str, content: str) -> None: ...   # "始终允许"→追加本地规则
```

**check 决策链**（对应 §3.1 五层）：

```
def check(tool, input) -> Decision:
    content = extract_content(tool.name, input)          # 见 §3.5 提取表

    # L1 黑名单（仅 Bash，硬拦截）
    if tool.name == "Bash" and any(p.search(content) for p in BLACKLIST):
        return DENY("检测到危险命令，已被安全策略硬拦截")

    # L4 bypassPermissions：跳过 L2-L5，黑名单仍生效
    if mode == BYPASS: return ALLOW("bypassPermissions 模式")

    # L2 路径沙箱（仅 filesystem 类）
    if tool.category == "filesystem" and not sandbox.contains(input.get("path")):
        return ASK("路径超出沙箱范围：" + input.get("path"))

    # L3 权限规则
    r = rule_engine.evaluate(tool.name, content)
    if r != UNKNOWN: return r

    # L4 权限模式矩阵（default/acceptEdits/plan → allow 或 ask）
    return mode_matrix[mode][tool]
```

### 3.3 第一层：危险命令黑名单

一组正则，命中直接 DENY，不经过任何规则/模式/确认。**只对 Bash 生效**——文件工具有路径沙箱守护。

| 正则模式 | 拦截原因 |
|---|---|
| `rm\s+-(([a-z]*r[a-z]*f\|[a-z]*f[a-z]*r)[a-z]*)\s+/\s*$` | 递归强制删除根目录 |
| `mkfs\.` | 格式化磁盘 |
| `dd\s+if=.*of=/dev/` | 直接写磁盘设备 |
| `chmod\s+-R\s+777\s+/` | 递归修改根目录权限 |
| `:()\{ :\|:& \};:` | fork bomb |
| `curl\s+.*\|\s*(ba)?sh` | 管道执行远程脚本 |
| `wget\s+.*\|\s*(ba)?sh` | 管道执行远程脚本 |
| `>\s*/dev/sd` | 覆盖磁盘设备 |

> **平台注意（Windows）**：上述正则按 bash 语法编写，默认覆盖 bash 类 shell；若默认 shell 为 PowerShell/cmd（`Remove-Item -Recurse -Force C:\`、`format` 等），需补充平台相关模式。**初版按配置化的 shell 类型加载对应模式集**（bash / powershell / cmd），默认在 Windows 加载 PowerShell 模式集。

被拦截返回 `操作被拒绝：检测到危险命令 "…"（可能造成不可逆损坏，已被硬拦截）`。

### 3.4 第二层：路径沙箱

**目的**：文件系统扁平，项目目录到 `/etc/passwd`（Windows：`C:\Windows\system32`、`.ssh/id_rsa`）只差一个路径。防模型被诱导读写项目之外、以及 **symlink/junction 逃逸**（目录内建链接指到项目外，字面路径检查会被骗过）。

```
def contains(requested, allowed_roots) -> bool:
    abs = to_absolute(requested)              # 相对路径 → 绝对
    real = resolve_symlinks(abs)              # 解析符号链接/联接点（Windows junction）
    if real 解析失败:                          # 文件不存在（WriteFile 新建场景）
        parent = resolve_symlinks(parent_dir(abs))
        if parent 也失败: return False
        real = parent / basename(abs)         # 查父目录真实路径，新建也在沙箱内
    return any(real.is_relative_to(root) for root in allowed_roots)
```

- **允许目录**：项目根目录（`work_dir`）+ 系统临时目录 + 配置白名单。Windows 注意路径大小写不敏感（比较前 `casefold`）。
- **越界 → ASK**（进 HITL，用户可批准单次；`bypassPermissions` 除外）。
- 只对 `category == "filesystem"` 的工具生效；`Bash` 中的路径操作由 OS 层才管得住——可控档没有 OS 层，属已知局限（见 §1）。

### 3.5 第三层：权限规则

**语法** `ToolName(pattern)`，`pattern` 为 glob 通配。从工具输入提取「内容」做匹配：

| 工具 | 提取字段 | 示例 |
|---|---|---|
| Bash | `command` | `git commit -m "fix bug"` |
| ReadFile / WriteFile / EditFile | `path` | `/proj/src/main.py` |
| Glob | `pattern` | `**/*.py` |
| Grep | `pattern` | `TODO` |

```yaml
# {work_dir}/.kdagent/permissions.yaml   （项目级，进版本控制，团队共享）
- rule: Bash(git *)            effect: allow
- rule: Bash(git push --force*) effect: deny
- rule: ReadFile(*.env*)       effect: deny
- rule: EditFile(*.py)         effect: allow
```

**三份规则文件，无优先级，合并裁决**：

| 文件 | 位置 | 用途 |
|---|---|---|
| 用户级 | `~/.kdagent/permissions.yaml` | 跨项目个人偏好，兜底通用规则 |
| 项目级 | `{work_dir}/.kdagent/permissions.yaml` | 团队共享，随仓库 review |
| 本地级 | `{work_dir}/.kdagent/permissions.local.yaml` | 不进版本控制；「始终允许」自动写入 |

```
def evaluate(tool_name, content):
    hit = UNKNOWN
    for rule in rules:                    # 三份合并成一个集合
        if not rule.matches(tool_name, content): continue
        if rule.effect == DENY: return DENY        # 最严，不可能被压过
        if rule.effect == ASK: hit = ASK
        elif hit == UNKNOWN: hit = ALLOW            # allow 最弱，仅未命中更严时记录
    return hit
```

- `deny > ask > allow`。**想禁死一个操作，写在哪一层都禁得死**，不会被下游 allow 顶掉；放宽权限只能改/删 deny。
- 层级不参与裁决，只决定「规则往哪写」。
- 规则文件不存在 → 按空规则集，新项目零配置可用。
- 内容提取对 `Bash` 的意义：`03` 阶段 Bash 只读/破坏性「保守声明」在此**升级为命令级动态判断**——`Bash(git push --force*)` 这类规则接管了单个命令的裁决（D10/T6 预留）。

### 3.6 第四层：权限模式

细粒度规则之外的整体信任档位。四模式覆盖「完全不信任 → 完全信任」光谱。

| 模式 | 只读工具 | 文件写工具 | Bash | 说明 |
|---|---|---|---|---|
| `default` | Allow | Ask | Ask | 日常开发：读随意，写/命令需确认 |
| `acceptEdits` | Allow | Allow | Ask | 信任改代码，命令仍谨慎 |
| `plan` | Allow | Ask | Ask | 与 default 矩阵一致；靠 Plan Mode 约束只读，模型不听话也会被 Ask 兜住 |
| `bypassPermissions` | Allow | Allow | Allow | 跳过 L2-L5，**黑名单仍生效**；无 OS 层兜底（Windows），危险，仅限 CI 等受控环境 |

- **默认模式 `default`**；`plan` 模式与 `05` 的 `/plan` 绑定（切换 Plan Mode 即切 plan 权限矩阵）。
- **`bypassPermissions` 不提供 UI 快捷切换**，仅配置文件显式开启（防误触）。
- 状态栏展示当前模式（`05` 已有 `[DEFAULT]/[PLAN]`）。

### 3.7 第五层：HITL 人在回路

L1-L4 均无法决策（规则未命中 + 模式说 Ask）→ 暂停 Loop，弹确认，人类拍板。**复用 `05` 的 ModalScreen 通道**。

```
05 ModalScreen 确认对话框：
  [Bash] git commit -m "fix: resolve null reference"
  允许执行？(y)是 / (n)否 / (a)始终允许此类操作
```

**阻塞交接**（Agent Loop 与 UI 异步协作，`02` 事件流扩展）：

```
async def ask_user(tool, input) -> Verdict:
    future = Future()
    emit(PermissionRequestEvent(tool_name, summary, future))   # 02 AgentEvent 新增
    verdict = await future                                     # Loop 阻塞，UI 回传
    if verdict == ALLOW_ALWAYS:
        checker.learn(tool.name, extract_content(tool.name, input))  # 追加本地规则
    return verdict
```

- **「始终允许」→ 权限学习循环**：自动生成一条 allow 规则追加到 `permissions.local.yaml`（带注释时间戳），同类操作下次直接放行，安全基线不降。
- 用户拒绝 → 与 DENY 同路径（见 §3.9）。

### 3.8 敏感路径禁写（应用层）

即使文件在沙箱内、规则全 allow，以下路径**绝对禁写**（模型改任一处即等于提权或自改指令）：

| 路径 | 禁写原因 |
|---|---|
| `{kdagent_dir}/config.yaml` | 含 API Key；可改模型参数、转发恶意服务器 |
| `{kdagent_dir}/permissions*.yaml` | 可自加 `Bash(*)→allow` 直接提权 |
| `{kdagent_dir}/skills/` | Skill 自动注入 system prompt，可写=可自改指令（prompt 注入） |

> `{kdagent_dir}` 取项目级 `.kdagent/` 与用户级 `~/.kdagent/` 两处（D7）。OS 层沙箱（生产级）会在此之上再禁写一遍，形成双重防护。

### 3.9 嵌入 02 Loop（`_exec_one` 扩展）

```
async def _exec_one(tool, input, ctx):                 # 02 §3.7 扩展
    errors = tool.validate_input(input)                # 03：参数校验
    if errors: return ToolResult(is_error=True, content=str(errors))

    decision = checker.check(tool, input)              # 06：五层裁决
    if decision.effect == DENY:
        return ToolResult(is_error=True, content="权限拒绝：" + decision.reason)
    if decision.effect == ASK:
        verdict = await ask_user(tool, input)          # 05 HITL
        if verdict not in (ALLOW, ALLOW_ALWAYS):
            return ToolResult(is_error=True, content="已被用户拒绝")

    reject = hooks.run_pre_tool(HookContext(tool, input))  # 06：pre_tool_use
    if reject: return ToolResult(is_error=True, content=reject.reason)

    result = await tool.execute(ctx, input)            # 03
    hooks.run("post_tool_use", HookContext(tool, input, result))  # 06
    return result
```

**拒绝不终止 Loop**——DENY 与用户拒绝都产出 `is_error=True` 的 ToolResult 进历史，模型看到「权限拒绝：…」后自行调整策略（如换 `git push` 为 `git commit` 提交、逐个删文件而非 `rm -rf`）。这是权限与 Loop 协作的核心：**被拒绝不等于停下来**。

### 3.10 Hook 系统

**三要素**：事件（什么时候）/ 条件 if（什么情况下）/ 动作 action（做什么）。

```yaml
# config.yaml 的 hooks 节（用户级/项目级/本地级追加合并）
hooks:
  - id: auto-format
    event: post_tool_use
    if: 'tool == "WriteFile" && args.path ~= "*.py"'
    action:
      type: command
      command: "black $FILE_PATH"
      timeout: 10s
```

**事件表**（完整枚举，可控档实现集标 ✅，其余预留）：

| 事件 | 时机 | 可控档 |
|---|---|---|
| `session_start` / `session_end` | 会话开/关 | ✅ |
| `turn_start` / `turn_end` | 轮次开/关 | ✅ |
| `pre_tool_use` / `post_tool_use` | 工具执行前/后 | ✅（前者可拦截） |
| `permission_request` | 权限审批请求时 | ✅（与权限系统联动） |
| `startup` / `shutdown` | 程序启动/退出 | ✅（与 07 结合） |
| `error` | 发生错误时 | ✅ |
| `compact` | 上下文压缩时 | ✅ |
| `pre_send` / `post_receive` | 消息发送前/响应后 | 预留 |
| `file_change` / `command_execute` | 文件变更/命令执行 | 预留 |

**`pre_tool_use` 是唯一能「说不」的事件**：设置 `reject: true` → 工具调用取消，Hook 输出作为错误结果返回模型，模型调整策略（反馈循环）。拦截逻辑必须同步执行、等待结果。

**顺序约定**：多个 Hook 匹配同一事件按 YAML 出现顺序执行；**任一 reject → 短路**，后续 Hook 不再跑（兜底拦截规则放前面）。`pre_tool_use` 的 `async: true` 与 `reject` 互斥（配置校验拦截）。

**条件语法**（复用权限规则匹配语法，免学两套）：

```
支持操作符：== 精确  != 反向  =~ 正则  ~= glob
组合：&& 与、|| 或；两者不可混用（避免引入表达式引擎）
字段：tool 工具名；event 事件名；args.xxx 工具参数（未知字段返回空串不报错）
```

**四种动作执行器**：

| 执行器 | 行为 | 可控档 |
|---|---|---|
| `command` | 执行 shell 命令，捕获输出/退出码，`timeout` 控制 | ✅ |
| `prompt` | 以 `<system-reminder>` 追加一条 user 消息注入提示词（不改 system prompt，不破坏前缀缓存，可被压缩回收） | ✅ |
| `http` | 发 HTTP 通知（Slack/收集系统/告警） | ✅ |
| `agent` | 启动子 Agent 自主决策（AI 监督 AI） | **预留**（依赖 `10` SubAgent 运行时，本模块只留接口骨架） |

**执行控制**：
- `once: true` —— 本次进程只触发一次（不持久化；重启=全新会话，语义如此）。
- `async: true` —— 后台执行不阻塞主流程（发通知等）；`pre_tool_use` 禁止。
- **错误兜底**：Hook 执行出错**只记日志、不中断 Agent 主流程**——辅助机制的故障不能反过来搞崩核心流程（尾巴摇狗）。

**上下文变量**：`$EVENT` `$TOOL_NAME` `$FILE_PATH` `$MESSAGE` `$ERROR` `$TOOL_ARGS.xxx`；未定义变量替换为空串，不报错。

**配置加载与校验**：
- 三处 `config.yaml`（用户级 `~/.kdagent/` / 项目级 `{work_dir}/.kdagent/` / 本地级 `config.local.yaml`）**追加合并**，全部生效（与权限规则同源）。
- 启动时集中校验：事件名合法；action 类型 ∈ {command,prompt,http,agent}；`reject` 仅限 `pre_tool_use`；`async` 禁用于 `pre_tool_use`；每类 action 必填字段（command 必须有 `command`，http 必须有 `url`）。非法配置报错并定位到具体 Hook id。

**接口**：

```python
# hooks/engine.py
@dataclass
class HookContext:
    event: str
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""

class HookEngine:
    def run(self, event: str, ctx: HookContext) -> None: ...     # 非拦截事件
    def run_pre_tool(self, ctx: HookContext) -> HookReject | None: ...  # 可拦截
```

**嵌入 02**（Agent.run 内）：

```
hooks.run("session_start", ctx)
loop:
    hooks.run("turn_start", ctx)
    response = llm.send(messages)                # pre_send/post_receive 预留于此处
    for tool_call in response.tool_calls:
        ... 权限裁决（§3.9）→ pre_tool_use → execute → post_tool_use
    hooks.run("turn_end", ctx)
hooks.run("session_end", ctx)
```

### 3.11 关键参数

| 参数 | 初值 | 含义 |
|---|---|---|
| 默认权限模式 | `default` | 全局可配置覆盖 |
| 黑名单模式集 | bash + powershell + cmd | 按默认 shell 加载 |
| 规则文件 | 用户级/项目级/本地级 三份 | 合并裁决，deny>ask>allow |
| Hook 事件集 | 10 个 ✅（§3.10 表） | 其余预留 |
| Hook 执行器 | command/prompt/http | agent 预留（`10`） |
| 敏感禁写路径 | config.yaml / permissions*.yaml / skills/ | 用户级+项目级两处 |

---

## 4. 参考实现

| 内容 | 参考 |
|---|---|
| 五层纵深防御（威胁模型/黑名单/沙箱/规则/模式/HITL/拒绝不终止） | [五层纵深权限防御](../mewcode设计文档/理论学习_五层纵深权限防御.md) |
| Hook 三要素/事件/pre_tool_use 拦截/条件语法/四执行器/once·async/错误兜底 | [Hook 生命周期钩子](../mewcode设计文档/理论学习_Hook生命周期钩子.md) |
| `require_confirm` 前置预留、`ToolContext` 依赖位 | `03` §3.2/§3.6 |
| ConfirmDialog HITL 通道、权限模式状态栏 | `05` §3.4/§3.2 |
| `_exec_one` 嵌入点、`is_error` ToolResult、AgentEvent | `02` §3.7/§3.6 |

---

## 5. 验收标准

- [ ] 黑名单 8 条模式命中（`rm -rf /`、`curl | bash` 等）→ DENY，返回「操作被拒绝」错误结果
- [ ] symlink/junction 指向沙箱外的路径 → 沙箱拦截并 ASK；WriteFile 新建文件（父目录在沙箱内）→ 放行
- [ ] 规则裁决正确：`Bash(git push --force*)` deny 压过 `Bash(git *)` allow；规则文件不存在按空集
- [ ] 四模式矩阵行为正确；`bypassPermissions` 下黑名单仍拦截
- [ ] ASK → 弹 ModalScreen 确认；y 执行 / n 拒绝返回 is_error / a 追加本地规则（重启后同类操作不再问）
- [ ] 权限拒绝不终止 Loop：模型下一轮看到拒绝原因并调整策略
- [ ] `pre_tool_use` + `reject: true` 拦截工具调用，拒绝原因作为 ToolResult 进历史
- [ ] `post_tool_use` 触发 `command` 动作（如写 .py 后跑 black），`$FILE_PATH` 正确替换
- [ ] `once: true` 本进程只触发一次；`async: true` 不阻塞主流程；Hook 报错只记日志、Agent 继续
- [ ] 写入敏感路径（config.yaml/permissions/skills）被禁写
- [ ] 非法 Hook 配置（reject 用于 post_tool_use 等）→ 启动时报错并定位到 hook id

---

## 6. TO-DECIDE

| # | 待决项 | 关联 |
|---|---|---|

> ✅ 已决（2026-08-12，D14/D15/D16）：**T17** → 可控档**不做 OS 级沙箱**，仅应用层五层防线；生产级再评估（Worktree 隔离可作部分替代）；OS 沙箱附带的断网/autoAllow 顺延。**T18** → Hook 实现 10 事件 + command/prompt/http 执行器；`pre_send`/`post_receive`、`file_change`、`command_execute`、`agent` 执行器留后续。**T19** → `05` 补 `/permissions` 命令（local-ui）；`bypassPermissions` 仅配置文件开启。
