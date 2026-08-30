"""SWE-bench-Live 任务加载（11 §3 benchmark 评测入口）。

数据源：HuggingFace `SWE-bench-Live/SWE-bench-Live`——持续更新的真实
issue 修复题（Python 生态，字段与 swe-bench 经典格式一致：instance_id /
repo / base_commit / problem_statement / patch / test_patch / FAIL_TO_PASS /
PASS_TO_PASS / test_cmds / log_parser）。

评测按「一 repo 一跑批」组织：每道题的 base_commit 落在同一源码 repo，
EvalRunner 以单个 repo 为封史来源（seal_copy）。本模块负责：

1. `fetch_split` 从 datasets-server rows HTTP API 拉全量（httpx，零新依赖），
   可选本地 JSON 缓存避免重复网络拉取；
2. `to_task_dict` / `build_tasks_json` 转成 `kdagent eval <tasks.json>`
   直接消费的结构（cli.load_tasks_file 忽略未知键，官方 test_cmds/test_patch
   随 tasks.json 保留给未来 Docker 判分后端读）。

判分现状（双轨，11 §3.2）：冒烟阶段 test_cmd 留空 → 走 gold_patch 相似度
回退；官方容器判分（test_cmds + docker_image）是 224 项工作，接好后在
EvalRunner._run_test 的 Docker 后端读取本模块保留的判分信息。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

DATASET = "SWE-bench-Live/SWE-bench-Live"
SPLITS = ("test", "lite", "verified", "full")
# datasets-server rows API（无需 HF token，公开数据集）。
_ROWS_URL = "https://datasets-server.huggingface.co/rows"


def fetch_split(
    split: str,
    *,
    repos: list[str] | None = None,
    instance_ids: list[str] | None = None,
    limit: int | None = None,
    cache_file: Path | None = None,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """拉取 split 全量原始行（分页 rows API）。

    - `repos`：按 `org/repo` 过滤（如 ["joke2k/faker"]）；
    - `instance_ids`：精确选题（复现指定题，冒烟挑最轻题）；
    - `limit`：过滤后截断（冒烟取前几道）；
    - `cache_file`：存在则直接读缓存跳过网络；否则拉全量并写缓存。
    """
    if split not in SPLITS:
        raise ValueError(f"未知 split {split!r}（可选 {SPLITS}）")
    if cache_file is not None and cache_file.is_file():
        rows: list[dict[str, Any]] = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        rows = _fetch_all_rows(split, page_size)
        if cache_file is not None:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    if repos is not None:
        want = set(repos)
        rows = [r for r in rows if r.get("repo") in want]
    if instance_ids is not None:
        want_ids = set(instance_ids)
        rows = [r for r in rows if r.get("instance_id") in want_ids]
        # 按请求顺序稳定排序（复现选题时报告/对比按题序展示）
        rows.sort(key=lambda r: instance_ids.index(r["instance_id"]))
    if limit is not None:
        rows = rows[:limit]
    return rows


def _fetch_all_rows(split: str, page_size: int) -> list[dict[str, Any]]:
    """分页拉全量；batch 不足 page_size 即到末尾。"""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = httpx.get(
            _ROWS_URL,
            params={
                "dataset": DATASET,
                "config": "default",
                "split": split,
                "offset": offset,
                "length": page_size,
            },
            timeout=120,
        )
        resp.raise_for_status()
        batch = [r["row"] for r in resp.json().get("rows", [])]
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def to_task_dict(row: dict[str, Any]) -> dict[str, Any]:
    """SWE-bench-Live 原始行 → tasks.json 单题条目。

    EvalTask 全字段一一映射；官方判分信息（test_cmds/test_patch/docker_image/
    log_parser）随题保留，`load_tasks_file` 只取已知键、其余忽略，未来 Docker
    判分后端直接读回。
    """
    return {
        "instance_id": row.get("instance_id", ""),
        "repo": row.get("repo", ""),
        "base_commit": row.get("base_commit", ""),
        "problem_statement": row.get("problem_statement", ""),
        "fail_to_pass": list(row.get("FAIL_TO_PASS") or []),
        "pass_to_pass": list(row.get("PASS_TO_PASS") or []),
        "gold_patch": row.get("patch", ""),
        # 本地双轨判分留空（走 gold_similarity 回退）；Docker 判分用下方官方信息。
        "test_cmd": "",
        "p2p_cmd": "",
        "constraint": "",
        # ---- 官方 harness 判分信息（224 Docker 判分后端读取）----
        "test_cmds": list(row.get("test_cmds") or []),
        "test_patch": row.get("test_patch") or "",
        "docker_image": row.get("docker_image") or "",
        "log_parser": row.get("log_parser") or "",
    }


def build_tasks_json(
    rows: list[dict[str, Any]],
    *,
    repo_dir: Path,
    work_dir: Path | None = None,
    run_id: str = "swebench-live",
) -> dict[str, Any]:
    """组装 `kdagent eval <tasks.json>` 直接消费的结构（含 run_id/repo_dir/work_dir）。"""
    return {
        "run_id": run_id,
        "repo_dir": str(repo_dir.resolve()),
        # D96 治理⑤：默认 work_dir = repo_dir（评测工作根），与 load_tasks_file 一致，
        # 避免 `.kdagent/eval/.kdagent/...` 双层冗余（RUNS.md 经验 6）。
        "work_dir": str((work_dir or repo_dir).resolve()),
        "tasks": [to_task_dict(r) for r in rows],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """CLI 参数：生成冒烟/正式评测的 tasks.json（clone 源码 repo 是前置手动步骤）。"""
    p = argparse.ArgumentParser(
        prog="python -m kdagent.eval.swebench",
        description="SWE-bench-Live 任务加载：拉取数据集并生成 `kdagent eval <tasks.json>` 配置",
    )
    p.add_argument("--split", default="test", choices=SPLITS)
    p.add_argument("--repo", action="append", default=[], help="过滤 repo（org/repo，可多次）")
    p.add_argument("--instance-id", action="append", default=[], help="精确选题（可多次，按给定顺序）")
    p.add_argument("--limit", type=int, default=None, help="取前 N 道（冒烟）")
    p.add_argument("--repo-dir", required=True, help="本地 clone 的源码 repo 目录（封史来源）")
    p.add_argument("--work-dir", default=None, help="eval 工作目录（默认 repo_dir/.kdagent/eval）")
    p.add_argument("--run-id", default="swebench-live", help="评测 run_id")
    p.add_argument("--out", required=True, help="tasks.json 输出路径")
    p.add_argument("--cache", default=None, help="数据集本地缓存 JSON（存在则读，否则拉取后写入）")
    return p


def main(argv: list[str] | None = None) -> int:
    """命令行入口：fetch → 转 tasks.json → 落盘。示例：

        python -m kdagent.eval.swebench \\
            --repo joke2k/faker --limit 3 \\
            --repo-dir D:/benchmark/swebench-live/repos/joke2k__faker \\
            --out D:/benchmark/swebench-live/tasks-faker.json --run-id faker-smoke
    """
    args = build_arg_parser().parse_args(argv)
    cache_file = Path(args.cache) if args.cache else None
    rows = fetch_split(
        args.split,
        repos=args.repo or None,
        instance_ids=args.instance_id or None,
        limit=args.limit,
        cache_file=cache_file,
    )
    if not rows:
        print("无匹配任务（检查 --repo/--split/网络）", file=sys.stderr)
        return 2
    data = build_tasks_json(
        rows,
        repo_dir=Path(args.repo_dir),
        work_dir=Path(args.work_dir) if args.work_dir else None,
        run_id=args.run_id,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {len(rows)} 题 -> {out}（run_id={args.run_id}，repo_dir={data['repo_dir']}）")
    for t in data["tasks"]:
        print(f"  {t['instance_id']}  F2P={len(t['fail_to_pass'])}  P2P={len(t['pass_to_pass'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
