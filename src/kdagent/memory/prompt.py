"""记忆使用说明（08 §3.5 主动线）：注入 system prompt，指导模型何时翻记忆。

主动线不发明专用 memory_search/save：模型用通用文件工具（ReadFile/Glob/Grep/
Write/Edit）翻 MEMORY.md 索引 → 读主题文件详情；写走两步保存法。这段提示词让
模型知道"何时该翻记忆、怎么翻、怎么写"。
"""

MEMORY_USAGE_INSTRUCTION = """## 记忆（KDAgent 四类 Markdown 文件）

记忆分四类 Markdown 文件（user/feedback/project/reference），MEMORY.md 是索引。
- 索引已随初始上下文加载（见上方 system-reminder）：回答涉及过往工作/决策/用户
  偏好/待办时，直接用 ReadFile 读索引指针指向的 `.md` 取详情，无需重新探索记忆目录。
- 身份与称呼是用户偏好：被问及「你叫什么名字/用户怎么称呼你/你叫什么昵称」时，
  **第一轮就必须 ReadFile `用户偏好.md`**（索引指针已是绝对路径）取约定再回答，
  禁止凭基础身份自我介绍（不得自称 Claude/Claude Code/通用助手）。
- 用户纠正/新偏好 → 写入 `feedback`/`user` 类记忆文件。
- 新增记忆两步保存：先写主题文件，再在 MEMORY.md 加一行索引指针。
"""
