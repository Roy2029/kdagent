### Harness
上下文精细化管理 — 想得清楚
工具系统集成 — 没有危险操作的条件
任务执行编排 — 正确地做事、做正确的事
记忆与状态管理 — 有记性、不走神
评估与观测 — 出错能发现
约束校验与失败恢复 — 错了能兜底

#### 上下文管理
- 渐进式披露（Progressive Disclosure）：不在 system prompt 里堆砌所有规则。核心索引放 prompt，详细规范放外部文件，Agent 按需 Read。OpenAI 的实践就是把工程规范写成 .md 文档，Agent 执行任务时自己去读对应章节。
- CLAUDE.md / 项目宪法：在项目根目录放一个 CLAUDE.md，作为 Agent 的"入职手册"——项目结构、技术栈、编码规范、关键约定。Agent 每次启动自动读取，建立项目心智模型。这就是 document-as-truth 理念的落地。
- 上下文线程化：不同任务使用独立上下文，互不污染。比如修 bug 和做 feature 分别开新的 Agent 实例，各自携带最小必要的项目背景。
- 上下文重启（Context Restart）：长任务中上下文接近爆满时，生成状态摘要，起一个新 Agent 接手继续，避免"上下文焦虑"导致行为退化。这是 Anthropic 的实践。

#### 工具系统
- 权限收敛（Sandbox）：不是给 Agent 完整的 shell 权限，而是通过工具白名单控制。比如只允许 git、npm、python，禁止 rm、chmod 777、curl | bash。Claude Code 的 permission 系统就是这个思路——敏感操作弹窗确认。
- 工具桥接（Tool Bridge）：对危险操作做二次封装。不是直接暴露 Edit 工具，而是要求先用 Read 读取文件、再用 Edit 精确替换——这样每次写入都有"读取确认"的前置校验。
- Schema 约束：工具参数用 JSON Schema 严格定义，限制输入范围。比如 Write 必须传入完整绝对路径 + 完整内容，不允许模糊的相对路径或追加模式。
- 工具使用可观测：每次工具调用都记录（工具名、参数、耗时、返回码），出问题时能回溯"Agent 哪一步走错了"。

#### 任务执行编排
Plan and Execute 模式：接到复杂任务后，Agent 先输出执行计划（Plan），用户确认后再逐步执行。Claude Code 的 Plan Mode（EnterPlanMode）就是这个实践——先规划、再实施。
任务分解：大任务按依赖关系拆成子任务，用 Task 工具跟踪状态（pending → in_progress → completed）。每个子任务有明确的完成标准，Agent 自检后再进入下一步。
Replan 机制：执行过程中发现计划不可行，触发 replan 而不是硬着头皮继续。比如依赖的库版本不兼容，立即调整方案而不是降级到有安全漏洞的旧版本。
Spec 驱动开发：用 Spec 文档定义"完成的标准"是什么，Agent 按 Spec 逐项实现，而不是自由发挥。这是人类驾驭 Agent 层面的关键手段。


#### 记忆与状态管理
- 经验库（Experience Repository）：把每次的教训沉淀为可检索的规则文档。比如"这个项目的 API 返回格式是 JSON:API 规范，不是 RESTful"写进 CLAUDE.md，下次 Agent 自动遵守。这就是 Mitchell Hashimoto 说的"修补沉淀到环境里"。
- 熵减机制：经验库需要持续维护——偶尔犯的规则压缩为一两句，频繁触发的硬骨头原样保留甚至加重语气，过期规则自动清理。避免经验库变成垃圾堆。
- 记忆类型分层：
    - 短期记忆 = 当前对话上下文
    - 中期记忆 = 项目级 CLAUDE.md / .claude/settings.json
    - 长期记忆 = 用户级 ~/.claude/CLAUDE.md（跨项目偏好）
状态传递：上下文重启时，把关键状态（已完成的任务、未解决的问题、当前的阻塞点）压缩成摘要传给新 Agent，而不是让新 Agent 从零开始。

#### 评估与观测
评估与生成分离（Evaluation-Generation Separation）：生成代码的 Agent 和检查代码的 Agent 必须是不同的实例（或至少不同的 prompt 角色）。Anthropic 的 Full Harness 架构就是 Planner + Generator + Evaluator 三元协作——写代码的人不评代码。
自动化测试：Agent 写完代码后强制运行测试套件。不是"建议运行测试"，而是把测试集成到工具链——写完函数 → 自动跑单测 → 不通过则自动修复 → 再跑 → 通过才标记完成。
Lint + 静态分析：代码生成后立即 lint，不符合项目规范的自动标记并要求修复。这属于机械化约束的范畴——不是靠 Agent "自觉"，而是靠系统强制执行。
可观测性三件套：
执行追踪：每一步工具调用都记录（谁、什么时候、做了什么、结果如何）
质量指标：PR 通过率、测试覆盖率变化、lint 违规数趋势
成本监控：每个任务的 token 消耗、API 调用次数
对抗性评审：让一个"批判性 Agent"专门挑刺——"这段代码有什么问题？边界情况处理了吗？安全漏洞呢？" 这是多层反馈循环中的即时反馈层。

#### 约束校验与失败恢复
Hook 系统：在关键节点注入校验逻辑。比如 pre-commit hook 自动跑 lint + 测试 → 不通过则拒绝提交。这不是 Agent 自觉做的事，而是系统级别的机械化约束。
失败重试 + 策略切换：工具调用失败时不是简单重试，而是切换策略。比如 npm install 失败 → 检查 node 版本 → 尝试 --legacy-peer-deps → 还是失败则检查 lockfile 冲突 → 生成诊断报告给用户。
断路器（Circuit Breaker）：Agent 连续失败 N 次后自动挂起，不允许无限重试烧 token。Claude Code 对循环调用工具的上限限制就是这个机制。
代码回滚：Agent 的每次修改都通过 git 追踪。如果引入 bug，可以精确回退到修改前的状态，而不是靠 Agent "手动恢复"。
错误模式沉淀：每次失败的根因写入经验库。「上次 Edit 工具因为 old_string 不精确而失败，原因是文件中有两个相同字符串 → 经验库规则：使用 Edit 前必须用 Grep 确认 old_string 唯一」。下次 Agent 自动遵守。
规则量化追踪：用 scoring function 量化 Agent 对每条规则的遵守程度。例如"是否先读文件再编辑"的遵守率从 70% 提升到 98%——可量化、可追溯的改进。