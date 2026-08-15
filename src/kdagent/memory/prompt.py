"""记忆使用说明（08 §3.5 主动线）：注入 system prompt，指导模型何时翻记忆。

主动线不发明专用 memory_search/save：模型用通用文件工具（ReadFile/Glob/Grep/
Write/Edit）翻 MEMORY.md 索引 → 读主题文件详情；写走两步保存法。这段提示词让
模型知道"何时该翻记忆、怎么翻、怎么写"。
"""

MEMORY_USAGE_INSTRUCTION = """## 记忆（KDAgent 四类 Markdown 文件）

记忆分四类 Markdown 文件（user/feedback/project/reference），MEMORY.md 是索引。
- 回答涉及过往工作/决策/用户偏好/待办时：先读 MEMORY.md 定位，再读对应 `.md` 取详情。
- 用户纠正/新偏好 → 写入 `feedback`/`user` 类记忆文件。
- 新增记忆两步保存：先写主题文件，再在 MEMORY.md 加一行索引指针。
"""
