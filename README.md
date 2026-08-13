# KDAgent

类 Claude Code 的 Coding Agent（Python 3.11+ / Textual）。

- 技术规格：`docs/技术规格/00-总览与路线图.md`
- 状态：阶段 8 实现期 · M0 工程骨架

## 开发

```bash
uv sync               # 安装依赖 + dev 工具链
uv run pytest         # 冒烟测试
uv run mypy src       # 类型检查
uv run ruff check .   # lint
uv run kdagent --version
```
