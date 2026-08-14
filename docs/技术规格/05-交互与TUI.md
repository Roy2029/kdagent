# Coding Agent 技术规格 · 05 交互与 TUI

> 状态：**已确认 v0.2**（2026-08-12）｜里程碑：阶段 2（能跑档）｜依赖：`02`（AgentEvent 流）、`04`（会话管理）、`03`（require_confirm）、`01`（/compact）｜被依赖：`06`（权限确认 UI）、`07`（可观测性可视化挂载点，阶段 3）

---

## 1. 目标与范围

**本模块是用户唯一看得见的部分**。它回答：Agent 怎么被"用起来"。

- **Textual TUI**：三区域布局（对话区 / 输入框 / 状态栏），参考 Claude Code 简洁 UI。
- **事件流消费**：`02` 的 AgentEvent 流 → UI 实时渲染，Agent 与 UI 完全解耦。
- **Slash Command**：`/` 开头的输入走快车道本地执行，不消耗 token、毫秒响应。
- **HITL 交互**：能跑档的极简 Y/N 确认（`03` require_confirm）、取消信号传播。

**不在本模块范围**：
- 事件流的**生产**（Agent 内部逻辑）→ `02`；本模块只消费
- 权限裁决逻辑 → `06`（本模块只提供确认 UI；`06` 到来后接完整权限规则）
- 可观测性数据采集/存储 → `07`（本模块只留可视化挂载点）
- 记忆/Skill 的 UI → 阶段 4（`08`/`09`）

---

## 2. 与相邻模块关系

```
        02 Agent（生产者）         03 工具 require_confirm
              │ AgentEvent 流              │ 确认钩子（注入 ToolContext）
              ▼                            ▼
   ┌────────────────────── 05 Textual TUI ──────────────────────┐
   │  ChatView ◀ 事件渲染        ConfirmDialog ◀ 极简 Y/N       │
   │  InputBar ◀ 用户输入/命令     StatusBar ◀ token/模式        │
   └───────┬──────────────────────────────────────────────┬────┘
           │ /compact（01）        /session（04）           │ 07 挂载点
           ▼                      ▼                       ▼
       ContextManager          SessionManager          Trace/Metrics
```

- **消费 `02`**：`Agent.run()` 的 AgentEvent 异步流 → 各 Widget 更新；Esc → `02` 的 CancelledError 干净退出。
- **调用 `04`**：`/session list/resume/new/delete` 走 `SessionManager`。
- **调用 `01`**：`/compact` 走 `ContextManager.manual_compact`。
- **为 `06` 预留**：`ConfirmDialog` 是权限确认（HITL）的最小实现，`06` 升级为完整权限规则后复用同一 UI 通道。
- **为 `07` 预留**：事件流经统一 sink（Trace 采集点）；状态栏 token 是 `07` 监控指标的最初形态。

---

## 3. 详细设计

### 3.1 TUI 布局（Textual Widget 树）

Claude Code 风格：Chat 占主区，todo/tools **有内容才展开**，输入框与状态栏固定底部。

```
KDApp(App)
 └─ MainScreen
     ├─ Header        # 应用名 + 当前模式（DEFAULT/PLAN）
     ├─ ChatView      # 消息流容器：user/system 用 Static，assistant 用 Markdown
     ├─ TodoRegion    # todo 面板（见 3.2b）：有 todo 时展开，空则收起（display=False）
     ├─ ToolRegion    # 工具调用活动条：有调用时展开，LoopComplete 后收起
     ├─ ChatInput     # TextArea（非 Input）：IME 组合输入支持更好；Enter 提交 / Shift+Enter 换行
     └─ StatusBar     # 状态栏（见 3.2）
```

- **ChatView**：流式 `text_delta` 累积到同一行 Static（纯文本实时显示），流结束/工具调用前一次性替换为 `Markdown` widget 渲染（代码块/加粗/列表着色）。Static 内容一律 `markup.escape`，防止 `[`/`]` 被当 markup 解析破坏布局（M1-e 收尾修复：逐词换行/错位重叠）。
- **输入框用 TextArea 而非 Input**（M1-e 收尾修复，参考 mewcode）：TextArea 对中文 IME 组合输入支持更完整；bindings 全部 `priority=True`，规避 Textual Screen 默认 `tab=app.focus_next` / App 层 `ctrl+c=退出` 抢键。
- 工具调用不在对话区刷屏——独立 `ToolRegion` 展示"正在执行 X"，结果折叠为一行，避免占满屏幕。

### 3.2 AgentEvent → UI 映射

| AgentEvent（`02`） | UI 行为 |
|---|---|
| `StreamTextEvent` | ChatView 追加文字增量（逐字效果） |
| `ToolUseEvent` | ToolRegion 显示"⚙ {name} {格式化参数}" |
| `ToolResultEvent` | ToolRegion 折叠展示（成功绿/失败红 + 耗时） |
| `TestingEvent` | ToolRegion 显示"正在跑测试…"；passed 绿勾 / failed 红叉（失败归因可展开）/ regression_detected 黄警示（`12` TestRunner） |
| `TurnCompleteEvent` | 标记一轮结束 |
| `LoopCompleteEvent` | ChatView 滚动到底，收起 ToolRegion |
| `UsageEvent` | StatusBar 更新 token（`input / output / cache`） |
| `ErrorEvent` | ChatView 红色错误提示 + /help 引导 |
| `CancelledEvent` | ChatView 系统消息"已取消" |

**状态栏**（`07` 指标的最初形态）：

```
[DEFAULT] tokens: 45,230/200k | 工具 7 | /help 查看命令
[PLAN]    tokens: 45,230/200k | 只读探索 | /plan 退出规划
```

### 3.2b TodoRegion（todo 面板）

`03` TodoWrite → `04` SessionRecord.todos → 本模块从**会话状态**读渲染（非从 LLM 文本解析，`03` §3.6）。展示 todo → task → steps 列表，completed 打勾；`12` 检查点触发时高亮当前步骤（`12` §3.3 快照绑定）。

- 数据源：`SessionRecord.todos`（`04` §3.2），TodoWrite 每次调用后实时更新。
- **只读展示**：todo 更新只经 TodoWrite 工具，UI 不提供手动编辑（避免双写源）。

### 3.3 用户输入与取消

```
def handle_enter(input):
    if input.strip() == "": return          # 空输入不发 API，防误触
    input_bar.value = ""
    name, args, is_cmd = parse_command(input)
    if is_cmd: dispatch_command(name, args); return
    run_agent(input)                        # 消费事件流更新 UI

async def run_agent(text):
    async for ev in agent.run(text):
        ...  # 按 3.2 表格分发
```

**取消映射**：
- **Esc**（输入框焦点）：取消当前 Agent 循环，程序不退，可继续输入新问题 → `02` 捕获 `CancelledError` 干净退出、部分文本已落库。
- **Ctrl+C**：退出整个程序（Textual 绑定二次确认避免误退）。

### 3.4 确认对话框（能跑档极简 Y/N）

`03` 的 `require_confirm=True` 工具（WriteFile/EditFile/Bash）执行前弹确认。这是 `06` 权限 HITL 的最小形态，`06` 到来后接完整权限规则、复用同一 UI 通道。

```
02 _exec_one 查 require_confirm=True
  → 注入的确认钩子（由 05 UI 提供）→ push_screen(ConfirmDialog(tool, input))
  → yes：继续执行
  → no：返回 is_error=True 的 ToolResult（"已被用户拒绝"），模型下轮自行调整
```

- 钩子通过 `03` 的 `ToolContext` 注入（`03` 已预留 permission 依赖位），02 无需知道 UI 存在。
- 键盘操作（M1-i）：**y/n 键直选**（不依赖焦点），←/→ 在按钮间移动焦点；Esc 默认关闭返回 None（= 拒绝）。
- 能跑档不落盘权限规则；`06` 升级后此路径替换为 `deny > ask > allow` 裁决 + 权限学习。

### 3.5 Slash Command 框架

**定位**：让"清屏、查 token、切模式"这类无需 AI 的操作绕过 Agent Loop——杀鸡焉用牛刀。解决注册、解析、执行三大问题。

```python
# ui/commands.py
@dataclass
class Command:
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    usage: str = ""
    type: Literal["local", "local-ui", "prompt"] = "local"
    arg_prompt: str = ""          # 缺参时的提示，比"参数缺失"友好
    hidden: bool = False
    handler: Callable[[CommandContext], None]

@dataclass
class CommandContext:             # 依赖注入背包，隔离 UI 实现细节
    args: str
    agent: Agent
    conversation: ConversationManager
    session: Session
    ui: UIController
    config: Config

class UIController(Protocol):     # 命令不感知 UI 框架
    def add_system_message(self, text: str): ...
    def send_user_message(self, text: str): ...
    def set_plan_mode(self, enabled: bool): ...
    def get_token_count(self) -> int: ...
    def refresh_status(self): ...

class CommandRegistry:
    def register(self, cmd: Command) -> None: ...   # 启动时声明式注册
    def find(self, name: str) -> Command | None: ...  # 先命令名后别名
    def complete(self, prefix: str) -> list[str]: ... # Tab 补全
    # 读写锁：为 09 Skill 运行时动态注册预留
```

**三类命令**：
| 类型 | 行为 | 例子 |
|---|---|---|
| `local` | 不走 Agent Loop，handler 本地干完，结果以系统消息显示 | /help /status /compact /session /exit |
| `local-ui` | 同样本地执行，但改变 UI 行为状态 | /clear（重置界面） /plan（切模式） |
| `prompt` | 构造预设 prompt 转交 Agent（消耗 token） | /review（依赖 git，后续） |

> 注意："local 不走 Loop" ≠ "不碰 LLM"——`/compact` 内部调 LLM 生成摘要，但那是 handler 自管的单次调用，不经过 Loop 多轮推理。

**解析**：`/` 开头 → 去掉 `/` → 首个空格拆分 → 命令名小写（`/Help` = `/help`）。只输入 `/` → 列出可用命令。

**拦截时机**：在消息发给 Agent 之前。输入非命令 → Agent；是命令 → 命令系统，不发 API。

**用户细节**：
- 未知命令 → `未知命令：/{name}，输入 /help 查看可用命令`（错误永远带引导）
- 需参命令缺参 → 显示 `arg_prompt`
- **别名冲突启动时检测**，直接报错退出（别等用户用时行为不确定）
- **Tab 补全**：`/` 后列出命令；前缀匹配（命令名+别名）；单选直接补全、多选下拉

### 3.6 内置命令清单（能跑档 7 个 + 可控档追加）

| 命令 | 别名 | 类型 | 说明 |
|---|---|---|---|
| `/help` | `/h` `/?` | local | 列出命令；`/help <cmd>` 看详情 |
| `/status` | `/s` | local | 模式 / token / 工具数 / 工作目录 / 会话 id |
| `/compact` | `/c` | local | 手动压缩（01）；带参指定保留重点；<5K token 提示"无需压缩"；显示前后对比 |
| `/clear` | — | local-ui | 新会话（旧会话由 04 保存，可 /session 恢复） |
| `/plan` | `/p` | local-ui | Plan Mode toggle；带参时同时作为任务描述发送 |
| `/session` | — | local | `list / resume <id> / new / delete <id>` |
| `/exit` | — | local | 退出程序（或 Ctrl+C） |
| `/permissions` | `/perm` | local-ui | 权限模式查看/切换（default/acceptEdits/plan）；bypass 仅配置文件开启（06 可控档追加，T19/D16） |

不在能跑档：`/review`（依赖 Git 工具，后续）、`/memory`（阶段 4）。

### 3.7 会话恢复入口（`04`）

`/session` 无参 → 当前会话概要（id / 消息数）；`/session list` → 历史列表（创建时间 + 最后活跃，04 已提供数据）按活跃倒序；`/session resume <id>` → 恢复（走 `04` 恢复四步）；`/session new` / `delete <id>`。

### 3.8 可观测性挂载点（`07` 预留）

- 事件流经过统一 sink：`05` 渲染的同时写入 Trace 采集点（`07` 阶段启用）。
- 状态栏 token 展示是 `07` 监控指标的最初形态，`07` 阶段扩展为指标面板 / 实时活动流。

---

## 4. 参考实现

| 内容 | 参考 |
|---|---|
| TUI 三区域布局、状态栏、流式渲染 | [LLM API 与对话管理器](../mewcode设计文档/理论学习_LLM_API与对话管理器.md)（章末 TUI 小节） |
| AgentEvent 事件流与 UI 消费 | `02` §3.6；[ReAct 范式与 Agent Loop](../mewcode设计文档/理论学习_ReAct范式与Agent%20Loop.md) |
| Slash Command 框架（注册/解析/执行/三类/别名/补全/九命令） | [Slash Command 命令框架](../mewcode设计文档/理论学习_Slash%20Command命令框架.md) |
| 确认对话框/权限 HITL 形态 | `03` §3.6；`06`（阶段 3 接完整权限） |

---

## 5. 验收标准

- [x] Textual App 启动，布局渲染正常（Claude Code 风格：Chat 主区 + todo/tools 有内容才展开 + 底部输入/状态）；关闭退出干净（M1 收尾 ✅ 2026-08-14）
- [x] 流式 `text_delta` 累积显示不逐词换行、无错位重叠；`LoopCompleteEvent` 后滚动到底；助手消息渲染 Markdown（代码块/加粗/列表着色）（M1 收尾 ✅ 2026-08-14）
- [x] 工具调用在 ToolRegion 显示"正在执行 X..."，结果折叠展示；LoopComplete 后收起（M1 收尾 ✅）
- [x] 状态栏随 `UsageEvent` 实时更新 token（M1-e ✅）
- [x] Esc 取消当前循环干净退出、可继续输入；Ctrl+C 退出程序（M1-e ✅）
- [x] `require_confirm` 工具执行前弹 Y/N（居中弹窗）；选 no → 返回拒绝结果，Loop 继续；y/n 键直选、方向键切换按钮；长命令参数**截断显示**、dialog 高度自适应，按钮不被挤出（M1-e + M1 收尾 + M1-i ✅；居中须用具体类选择器 `ConfirmDialog, ExitDialog { align: center middle }`，基类 `Screen`/`ModalScreen` 选择器被 Textual 8.2.8 覆盖不生效）
- [x] `/help /status /compact /clear /plan /session /exit` 全部可用；只输入 `/` 列出命令（M1-e ✅）
- [ ] `/permissions` 查看/切换权限模式、显示规则统计（06 接入）
- [x] `/session list/resume/new/delete` 正确操作 `04` 会话；resume 后对话历史渲染回 ChatView（M1-f + M1 收尾 ✅）
- [ ] `/compact` 显示前后 token 对比；<5K 提示无需压缩
- [x] TodoRegion 从 `SessionRecord.todos` 实时渲染 todo → task → steps（非从 LLM 文本解析）；空时收起（M1-f ✅）
- [ ] `TestingEvent` 渲染"正在跑测试…"及 passed/failed/regression_detected 三态（`12` TestRunner）
- [x] Tab 补全：`/` 列命令、前缀补全（ChatInput priority 绑定，规避 Screen focus_next 抢键）（M1-e + M1 收尾 ✅）
- [x] 别名冲突 → 启动时报错；未知命令 → 带 /help 引导（M1-e ✅）
- [x] 输入框 TextArea：支持中文 IME 组合输入（Enter 提交、Shift+Enter 换行）；聚焦时 Ctrl+C 复制 / Ctrl+V 粘贴，clipboard 经 pyperclip 接**系统剪贴板**（Textual 默认进程内，M1-i 修复）；`ChatInput` **状态机过滤**鼠标序列残留（完整 `\x1b[<...M` 被 Textual 解析为 MouseEvent，但缺 ESC 前缀的 `[<35;56;28m` 会被拆成逐字符 Key 插入——状态机按 `[<数字;数字→m/M` 闭合判定，整段丢弃；`[` 正常输入延迟一个字符回补，M1-i 修复）；M1-i2 修复输入框三项：**backspace 兜底**（conhost 传统 backspace 发 `\x08`→Textual 解析为 ctrl+h，TextArea 仅 backspace(=\x7f) 绑定 → ChatInput 加 `ctrl+h→delete_left` priority 兜底，两路可删）、**ctrl+c/ctrl+v 显式 priority 绑定**（防 App 层 `request_quit` 抢键）、`_CONTROL_RE` 收窄为 `[\x1b\x9b]`（原 `[\x00-\x1f\x7f-\x9f]` 误杀 backspace 与中文 UTF-8 续字节 `\x80-\x9f`）（M1-i ✅ + M1-i2 ✅）
- [x] 中文 IME 环境根因与**传统输入层对齐**（M1-i2，用户环境修正）：**用户终端 = conhost（cmd 窗口），未装 Windows Terminal**（此前误认为 WT）。conhost **永远不支持** Kitty 键盘协议（WT 1.25 Preview 2026-03 才加入）——故方案 A（禁 `\x1b[>1u`）在 conhost 下是 no-op，中文失败与 Kitty 无关。**真正根因**：Textual 的 `enable_application_mode` 把 stdin 强制设 `ENABLE_VIRTUAL_TERMINAL_INPUT`（[win32.py:157](../.venv/Lib/site-packages/textual/drivers/win32.py#L157)），**conhost 在 VT 输入模式下对中文 IME 组合提交处理有缺陷** → 中文失败；而 Claude Code（Node/ink）不设该模式、走传统 `ReadConsoleInputW` 直读 Unicode 字符，故同 cmd 中文正常。对齐方案（用户选定「坚持 cmd」）：`src/kdagent/compat.py` monkeypatch（win32 生效、幂等、`cli.main()` 启动前调用）① 过滤 `\x1b[>1u`；② `enable_application_mode` → 传统事件输入模式（即时读键无 LINE/ECHO/PROCESSED + 窗口/鼠标事件）；③ `EventMonitor.run` 重写——KEY_EVENT 用 `translate_key_event`（UnicodeChar 优先=IME 汉字直读，VK 码→xterm 序列：方向键/编辑键/功能键/backspace/enter/tab/esc），MOUSE_EVENT 用 `translate_mouse_event` → SGR 鼠标序列。⚠️ 第一轮实测：**其他键全 OK，仅中文失败** → 二次根因：conhost 传统模式 IME 确认汉字以 **VK=0 + UnicodeChar** 的 KEY_EVENT 提交，而 event monitor 沿用原 VT 模式过滤 `dwControlKeyState and vk==0` 误杀该事件 → 修复：`_should_drop_key_event`（仅丢"无 VK 且无字符"空事件）+ `KDA_INPUT_DEBUG=1` 诊断开关（KEY_EVENT 原始字段写 `.kdagent/input-debug.log`）→ 待实测

---

## 6. TO-DECIDE

| # | 待决项 | 关联 |
|---|---|---|

> ✅ 已决（2026-08-11，D13）：**T15** → 内置命令 **7 个全保留**（含 `/plan`、`/exit`）；**T16** → 确认对话框用 **ModalScreen**（Textual 模态，Y/N 清晰）。
