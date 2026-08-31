---
name: swebench-loop
description: SWE-bench-Live 评测迭代闭环。当需要跑 benchmark 评测批次、收集/分析 badcase（失败实例 trace）、迭代 KDAgent 版本、或做复测对比（同批题不同版本 AC 率）时使用。涵盖五环：跑批 → 收 badcase → 分析 → 迭代 → 复测，全部命令与文件路径已记录，可直接执行。
license: MIT
metadata:
  author: kdagent
  version: "1.0"
---

# SWE-bench-Live 评测迭代闭环

对 KDAgent 做「跑测评数据集 → 收集 badcase → 分析问题 → 迭代版本 → 复测」的版本管理闭环。所有命令已实测，路径为绝对路径，可直接复制执行。

## 闭环总览（五环）

```
环1 跑批        kdagent eval <tasks.json> --docker-harness <run_harness.py> [--workers N] [--preinstall]
  ↓ 产物 report.json + obs/traces/*.jsonl
环2 收 badcase  kdagent eval <tasks.json> --report <run_id> 复核 / --annotate 批注 / 更新 RUNS.md
  ↓
环3 分析        python analyze_traces.py 批量信号 + 精选深挖 trace → 分类报告 + 迭代建议清单
  ↓
环4 迭代        一次只改一个变量 → git commit 绑定版本 → uv run python -m pytest
  ↓
环5 复测        同批基准题重跑（新 run_id）→ kdagent eval <tasks.json> --diff <旧> <新> 对比
```

## §0 前置环境

- **关键路径**（本机固定）：
  - benchmark 根：`D:/个人开发/benchmark/swebench-live/`
  - 官方 harness：`D:/个人开发/benchmark/swebench-live-harness/run_harness.py`
  - 封史源码仓库：`D:/个人开发/benchmark/swebench-live/repos/<org>__<repo>/`（如 `falconry__falcon`）
  - 数据集缓存：`D:/个人开发/benchmark/swebench-live/swebench-live-test.json`
  - KDAgent 项目：`d:/个人开发/CodingAgent/`
- **路径格式**：一律用 `D:/...` 形式。**禁止**用 MSYS `/d/...`（Windows python 不认，B2 曾踩坑）。
- **Docker daemon**：判分前先 `docker ps`；未启动则
  `powershell.exe Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"`，等 daemon 就绪。
- **镜像探测**：选题前先 `python D:/个人开发/benchmark/swebench-live/probe_images.py <org/repo>` 确认镜像存在（DockerHub 对不存在 repo 返回 401，非 404）。无镜像的题白跑。
- **代理**：clone 源码走 `git -c http.proxy=http://127.0.0.1:7890 clone ...`（直连 SSL reset）。
- **评测命令在项目根执行**：`cd d:/个人开发/CodingAgent` 后再跑 `kdagent eval ...`（需 `.env` 的 DEEPSEEK_API_KEY）。

## 环1 跑批

1. **生成题单**（一 repo 一批 5 题，`source_repo` 单一约束）：
   ```
   cd d:/个人开发/CodingAgent
   python -m kdagent.eval.swebench \
     --repo <org/repo> \
     --repo-dir D:/个人开发/benchmark/swebench-live/repos/<org>__<repo> \
     --limit 5 \
     --cache D:/个人开发/benchmark/swebench-live/swebench-live-test.json \
     --out D:/个人开发/benchmark/swebench-live/tasks-<x>.json \
     --run-id eval-b<x>
   ```
2. **探测镜像**（§0），剔除无镜像题。
3. **跑批**：
   ```
   kdagent eval D:/个人开发/benchmark/swebench-live/tasks-<x>.json \
     --docker-harness D:/个人开发/benchmark/swebench-live-harness/run_harness.py \
     --workers 2 --preinstall
   ```
   - `--preinstall`：封史副本建 `.venv` + `pip install -e .`（失败不阻断），缓解「pip download 病 / 读全局 site-packages」两类环境误导。
   - `--workers 2`：并发跑批（曾触发偶发 KeyError 并发 bug，D96 已根治；遇崩题单题补跑 workers=1）。
   - **退出码**：0 全过 / 1 有失败（正常语义）/ 2 配置错误。exit 1 不是崩溃。
4. **产物**：`{work_dir}/.kdagent/eval/<run_id>/report.json`（判分报告）+ `{work_dir}/.kdagent/obs/traces/*.jsonl`（trace）。
5. **版本绑定（必做）**：跑批前记录本次 KDAgent 版本，写入 RUNS.md 批次表：
   ```
   git -C d:/个人开发/CodingAgent rev-parse --short HEAD   # → 记入「agent 版本」列
   ```
   模型当前 `deepseek-v4-flash`（config.yaml 可改）。没记版本 = 本轮结果不可归因。

## 环2 收 badcase

1. **复核失败题**（交互：题号展开 span 树 / f 过滤报错 / d 看事件详情）：
   ```
   kdagent eval <tasks.json> --report <run_id>
   ```
2. **人工修正归类**（自动归类不可靠时）：
   ```
   kdagent eval <tasks.json> --annotate <run_id> <task_id> <kind> --note "<原因>"
   ```
   合法归类：`not_located / wrong_fix / regression / harness_fault / constraint_conflict`。
3. **更新台账** `D:/个人开发/benchmark/swebench-live/RUNS.md`（闭环核心记录，每次跑批后必更）：
   - 批次表加一行（见 §6 模板，**agent 版本列必填**）；
   - 明细段：逐题结果 + 根因一句话；
   - 累计统计：总题数 / resolved / 解决率 / 失败归类计数；
   - 新发现写进「经验教训」段（编号递增）。

**失败归类口径**：
| 归类 | 含义 | 判定 |
|---|---|---|
| wrong_fix | 定位对、修法不对 | patch 应用成功 + P2P 过 + F2P 未过 |
| regression | 破坏回归 | 改测试文件（2419）或改源码把功能改坏致 P2P 全挂（2459/2248） |
| empty_patch | 没产出 patch | 提取为空 |
| harness_fault | 环境/判分故障 | 容器启动/依赖/patch 应用失败（CRLF 等） |
| not_located | 未定位到目标 | — |
| unresolved | patch 未解决 F2P | 冒烟期旧口径 |

## 环3 分析

1. **批量信号**（全 trace 结构化摘要 → trace-analysis.txt）：
   ```
   cd D:/个人开发/benchmark/swebench-live
   python analyze_traces.py
   ```
   每题输出：轮次 / 改源次数 / 测跑次数 / 工具分布 / 错误分类 / 最后动作 / stop_reason。
   关注模式：成功题测跑 7-12 次 vs 失败题 0-4 次（**验证不足**是最大共性）；stop_reason 是否 max-iterations 跑满；错误分类里的环境误导。
2. **精选深挖**：挑代表性失败实例读 trace 原始 jsonl（`repos/<repo>/.kdagent/obs/traces/*.jsonl`），定位根因（如某题改 9 次测 2 次就交卷、LLM 读全局 site-packages）。
3. **产出固定两份**：
   - **失败模式分类报告**：按归类分组，每题「改源/测跑/轮次/错误/最后动作」，总结共性模式。
   - **迭代建议清单**：按影响排序（prompt 约束 > 工具增强 > 基建/环境 > 观测口径），每条标注预计解决哪类失败。

## 环4 迭代

1. **一次只改一个变量**（prompt / 工具 / max_tokens / preinstall 等，只动一个，否则复测无法归因）。
2. **git commit 绑定版本**（版本号即 commit hash）：
   ```
   cd d:/个人开发/CodingAgent
   git add -A && git commit -m "feat(eval): <改动说明>"
   git rev-parse --short HEAD   # 记录到复测时使用的版本
   ```
3. **回归测试**：`uv run python -m pytest`（**必须用 uv**——本机 python 缺 pyperclip 导致 UI 测试 collection 失败）。
4. 新发现的边界情况写回 RUNS.md「经验教训」。

## 环5 复测

1. **准备基准题集**（已生成，固定题集）：
   - `D:/个人开发/benchmark/swebench-live/tasks-baseline-babel.json`（5 题：1075/1104/1126/1131/1194）
   - `D:/个人开发/benchmark/swebench-live/tasks-baseline-falcon.json`（8 题：2254/2419/2498/2477/2459/2450/2366/2248）
   - 新增基准集：`python make_baseline.py --from <tasks-*.json...> --ids <instance_id...> --run-id baseline-<repo> --out tasks-baseline-<repo>.json`（每 json 只能含同一 repo）。
2. **同批题重跑**：run_id 来自 tasks.json 字段，复测须用新 run_id——用 make_baseline 重新生成带 `--run-id baseline-<repo>-v<N>`，或直接编辑 json 的 `run_id` 字段。然后：
   ```
   kdagent eval D:/个人开发/benchmark/swebench-live/tasks-baseline-<repo>.json \
     --docker-harness D:/个人开发/benchmark/swebench-live-harness/run_harness.py \
     --workers 2 --preinstall
   ```
3. **对比**（一次一变量，看具体现象还在不在）：
   ```
   kdagent eval <tasks-baseline.json> --diff <旧run_id> <新run_id>
   ```
   输出 fail2pass（修复）/ pass2fail（回归）/ fail2fail（现象还在）/ pass2pass。
4. **报表**：`kdagent eval <tasks-baseline.json> --metrics <run_id>`（token / 成本 / 通过率）。
5. **归类迁移评估**（复测结论的核心）：
   | 迁移 | 判定 |
   |---|---|
   | empty_patch → wrong_fix | 进步（产出 patch，虽未解决） |
   | wrong_fix → resolved | 解决（F2P 实测过） |
   | resolved → 失败 | 回归（优先排查） |
   | fail2fail | 现象还在，稳定失败（非变化） |
6. 成本参考：falcon 8 题约 2.5 元/轮，babel 5 题约 1.5 元/轮。

## §6 台账模板与文件地图

**RUNS.md 批次表模板**（新增列 `agent版本`）：
```
| run_id | 日期 | repo | 题数 | resolved | 解决率 | 失败归类 | agent版本 | 备注 |
```
`agent版本` 填 `git -C d:/个人开发/CodingAgent rev-parse --short HEAD`（可加模型名）。

**评测代码**（KDAgent，`d:/个人开发/CodingAgent/src/kdagent/eval/`）：
| 文件 | 职责 |
|---|---|
| cli.py | eval 子命令分发：跑批/复核/批注/复测/报表 |
| runner.py | EvalRunner：封史副本、`extract_patch`、`_RUNTIME_DIRS`、preinstall |
| swebench.py | SWE-bench-Live 题单生成（fetch + tasks.json） |
| report_diff.py | `diff_runs`（复测对比）/ `metrics_by_run`（报表） |
| review.py | 失败复核 span 树 / 批注归类 |
| docker_judge.py | Docker 官方 harness 判分、patch 归一化 |
| trace_store.py / model.py | trace 存取 / 数据模型 |

**评测脚本/数据**（`D:/个人开发/benchmark/swebench-live/`）：
| 文件 | 职责 |
|---|---|
| RUNS.md | 台账（闭环核心记录） |
| analyze_traces.py | trace 批量信号 → trace-analysis.txt |
| probe_images.py | 镜像探测 |
| make_baseline.py | 基准题集生成 |
| rejudge-b5.py | 复用判分（不重跑 LLM，修判分后验证已跑 patch） |
| swebench-live-test.json | 数据集缓存 |
| repos/ | 6 repo 封史源（python-babel/babel、falconry/falcon、fonttools、urllib3、joke2k/faker、attrs） |
| work/ | 各题工作副本 |
| tasks-*.json | 各批题单 + `tasks-baseline-*.json` 基准集 |
| harness：`D:/个人开发/benchmark/swebench-live-harness/run_harness.py` | 官方判分入口 |

## §7 人工跑批速查（工程师一页卡，不依赖 agent）

```bash
# 1. 生成题单（一 repo 一批）
cd d:/个人开发/CodingAgent
python -m kdagent.eval.swebench --repo <org/repo> \
  --repo-dir D:/个人开发/benchmark/swebench-live/repos/<org>__<repo> \
  --limit 5 --out D:/个人开发/benchmark/swebench-live/tasks-<x>.json --run-id eval-b<x>

# 2. 探测镜像（无镜像题剔除）
python D:/个人开发/benchmark/swebench-live/probe_images.py <org/repo>

# 3. 跑批（记录版本先！）
git -C d:/个人开发/CodingAgent rev-parse --short HEAD
kdagent eval D:/个人开发/benchmark/swebench-live/tasks-<x>.json \
  --docker-harness D:/个人开发/benchmark/swebench-live-harness/run_harness.py \
  --workers 2 --preinstall

# 4. 复核 / 批注
kdagent eval <tasks.json> --report <run_id>
kdagent eval <tasks.json> --annotate <run_id> <task_id> <kind> --note "原因"

# 5. 分析
cd D:/个人开发/benchmark/swebench-live && python analyze_traces.py

# 6. 复测对比（同批基准题，改 json 的 run_id 为新值后重跑）
kdagent eval <tasks-baseline.json> --diff <旧run_id> <新run_id>
kdagent eval <tasks-baseline.json> --metrics <run_id>
```

## §8 常见坑（均已在 RUNS.md 有经验记录）

- **CRLF malformed patch**（3437/2322）：Windows 行尾 patch 容器 git apply 拒绝。D95 已治（docker_judge 写 patch 前归一化 LF）。新发现 CRLF 报错先查 docker_judge。
- **`.venv` 混入 patch**（B5 全挂）：给副本加运行时目录要同步进 `runner._RUNTIME_DIRS` 排除清单（当前含 `.kdagent`/`.claude`/`.venv`）。
- **并发 KeyError: 'status'**（3437/2404，~20% 触发）：D96 TodoWrite 无状态化已根治；再遇崩题单题补跑 workers=1。
- **pip download 病**（3679/3429）：LLM 弃工作区拉 PyPI 源码读。`--preinstall` 已缓解；trace 见 `pip download` 命令即此病。
- **读全局 site-packages**（1104 读 anaconda3）：模型 import 验证走全局而非工作区。prompt 环境说明已引导用 `.venv/Scripts/python.exe`。
- **Bash 写文件**（3441 `printf > _version.py`）：模型用 Bash 改文件而非 EditFile，改源统计会漏——分析时看 `git diff` 而非工具计数。
- **路径格式**：`D:/...`，禁 `/d/...`（MSYS）。`/testbed` 不存在，工作区是 Windows 绝对路径。
