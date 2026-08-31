# CodingAgent 项目指引

本项目是从零设计并开发一个类 Claude Code 的 **Coding Agent**（Python 3.11+ / Textual）。当前处于**实现期**：M0-M5 全档里程碑已关闭（v0.5.x），正在 SWE-bench-Live 评测迭代与整体 review 修复批次中。

## 当前状态与恢复

- **设计工作**记录在 `docs/技术规格/`，**主文档是 [docs/技术规格/00-总览与路线图.md](docs/技术规格/00-总览与路线图.md)**——包含进度跟踪、版本路线矩阵、文档地图、决策清单（已决/待决，含 D1-D104）、恢复指引。
- **若上下文已清空**：先读 `00-总览与路线图.md` 的「恢复指引」和「进度跟踪」，按当前阶段继续工作，不要从头开始。
- **评测台账**在 `D:/个人开发/benchmark/swebench-live/RUNS.md`（真实 LLM + Docker 判分逐批结果与经验教训）；评测闭环流程见 `.claude/skills/swebench-loop/` skill。
- 每次讨论结束后，**必须更新 `00` 文档的进度跟踪与决策清单**。

## 工程卫生（铁律）

- 每任务完成后**全量测试必须保持全绿**：`uv run pytest`（当前基线 953 passed + 6 skipped）、`uv run mypy src/kdagent`、`uv run ruff check src/kdagent tests`。
- 提交信息带 D 编号（决策编号见 00 §6.1）；一个逻辑改动一个 commit；文档与代码 commit 分离。

## 工作方式

- 阶段式推进：规格文档由我起草 → 用户 review → 修订 → 确认后进入下一阶段。中文交流，简洁直击要点。
- 局部问题可 dive-in 深入讨论，结论必须沉淀回对应模块文档与决策清单。

## 参考资料（设计时按需查阅）

- `docs/mewcode设计文档/` —— MewCode 课程学习笔记（14 篇），逐模块参考实现，先读 README.md 索引。
- `docs/子模块设计讨论与资料/` —— 用户的深化设计：记忆系统、可观测性工程、Harness 六层、长任务四方案、工具结果实时压缩建模。

## 已确认的关键决策（详见 `00` 文档 §6.1）

- 技术栈：Python 3.11+ + Textual
- 版本路线：五档（能跑/能用/可控/好用/生产级），M0-M5 全档已关闭
- 可观测性：本地轻量起步，Session/Trace/Span 数据模型，预留 OTel 接口（D80 已实装）
- 评估基准：SWE-bench-Live 为主（Docker 判分链路已通），BFCL/τ-Bench 为辅
