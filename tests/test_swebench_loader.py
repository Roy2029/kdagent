"""SWE-bench-Live loader 单元测试（benchmark 评测入口，D8x）。

覆盖：字段映射（SWE-bench 原始行 → tasks.json 单题）、tasks.json 结构、
fetch_split 分页/缓存/过滤（mock 网络）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from kdagent.eval.swebench import (
    DATASET,
    SPLITS,
    build_tasks_json,
    fetch_split,
    main,
    to_task_dict,
)


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


def _row(instance_id: str, repo: str = "joke2k/faker") -> dict[str, Any]:
    """一条最小 SWE-bench-Live 原始行（覆盖映射关心的字段）。"""
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": "abc123",
        "problem_statement": "issue 描述",
        "patch": "--- a/x.py\n+++ b/x.py\n",
        "test_patch": "--- a/test_x.py\n+++ b/test_x.py\n",
        "FAIL_TO_PASS": ["tests/test_x.py::test_fix"],
        "PASS_TO_PASS": ["tests/test_x.py::test_ok"],
        "test_cmds": ["pytest tests/test_x.py -v"],
        "log_parser": "default",
        "docker_image": "repolaunch/osbench:x",
    }


# ---- to_task_dict：字段映射 ----

def test_to_task_dict_maps_swebench_fields() -> None:
    d = to_task_dict(_row("joke2k__faker-2018"))
    assert d["instance_id"] == "joke2k__faker-2018"
    assert d["repo"] == "joke2k/faker"
    assert d["base_commit"] == "abc123"
    assert d["problem_statement"] == "issue 描述"
    assert d["gold_patch"] == "--- a/x.py\n+++ b/x.py\n"
    assert d["fail_to_pass"] == ["tests/test_x.py::test_fix"]
    assert d["pass_to_pass"] == ["tests/test_x.py::test_ok"]
    # 本地双轨判分留空 → gold_similarity 回退（Docker 判分是后续 224 项工作）
    assert d["test_cmd"] == ""
    assert d["p2p_cmd"] == ""
    assert d["constraint"] == ""


def test_to_task_dict_keeps_official_harness_info() -> None:
    """官方判分信息随题保留（未来 Docker 判分后端读取；load_tasks_file 忽略未知键）。"""
    d = to_task_dict(_row("x"))
    assert d["test_cmds"] == ["pytest tests/test_x.py -v"]
    assert d["test_patch"].startswith("--- a/test_x.py")
    assert d["docker_image"] == "repolaunch/osbench:x"
    assert d["log_parser"] == "default"


def test_to_task_dict_handles_missing_optional_fields() -> None:
    d = to_task_dict({"instance_id": "x"})
    assert d["fail_to_pass"] == []
    assert d["pass_to_pass"] == []
    assert d["gold_patch"] == ""
    assert d["test_cmds"] == []
    assert d["test_patch"] == ""


# ---- build_tasks_json：结构 ----

def test_build_tasks_json_shape(tmp_path: Path) -> None:
    rows = [_row("a"), _row("b")]
    data = build_tasks_json(rows, repo_dir=tmp_path, work_dir=tmp_path / "work", run_id="smoke-1")
    assert data["run_id"] == "smoke-1"
    assert data["repo_dir"] == str(tmp_path.resolve())
    assert data["work_dir"] == str((tmp_path / "work").resolve())
    assert [t["instance_id"] for t in data["tasks"]] == ["a", "b"]


def test_build_tasks_json_default_work_dir(tmp_path: Path) -> None:
    """D96 治理⑤：默认 work_dir = repo_dir（评测工作根），不再拼 `.kdagent/eval`。"""
    data = build_tasks_json([_row("a")], repo_dir=tmp_path)
    assert data["work_dir"] == str(tmp_path.resolve())


# ---- fetch_split：分页 / 缓存 / 过滤（mock httpx）----

def _fake_get_factory(pages: list[dict[str, Any]]):
    def _fake_get(url: str, *, params: dict[str, Any] | None = None, timeout: float = 120.0) -> _FakeResp:
        offset = int((params or {}).get("offset", 0))
        idx = offset // 100
        if idx < len(pages):
            return _FakeResp(pages[idx])
        return _FakeResp({"rows": []})
    return _fake_get


def test_fetch_split_paginates_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """两页数据：第一页 100 满（含 faker），第二页不足 100（faker 之外的 repo）。"""
    rows = [_row(f"joke2k__faker-{i}", repo="joke2k/faker") for i in range(100)]
    rows.append(_row("other__repo-1", repo="org/other"))
    pages = [
        {"rows": [{"row": r} for r in rows[:100]]},
        {"rows": [{"row": rows[100]}]},
    ]
    monkeypatch.setattr(httpx, "get", _fake_get_factory(pages))
    got = fetch_split("test", repos=["joke2k/faker"], limit=3)
    assert [r["instance_id"] for r in got] == [
        "joke2k__faker-0",
        "joke2k__faker-1",
        "joke2k__faker-2",
    ]
    got_all = fetch_split("test")
    assert len(got_all) == 101


def test_fetch_split_unknown_split_rejected() -> None:
    with pytest.raises(ValueError):
        fetch_split("nosuch")
    assert "test" in SPLITS


def test_fetch_split_instance_ids_keep_requested_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--instance-id` 精确选题且按请求顺序稳定排序（复现冒烟题）。"""
    rows = [_row("a", repo="r1"), _row("b", repo="r2"), _row("c", repo="r1")]
    monkeypatch.setattr(
        httpx,
        "get",
        _fake_get_factory([{"rows": [{"row": r} for r in rows]}]),
    )
    got = fetch_split("test", instance_ids=["c", "a"])
    assert [r["instance_id"] for r in got] == ["c", "a"]
    # repo 过滤与 instance_ids 可叠加
    got2 = fetch_split("test", repos=["r1"], instance_ids=["c", "b", "a"])
    assert [r["instance_id"] for r in got2] == ["c", "a"]


def test_fetch_split_uses_cache_and_skips_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "rows.json"
    cache.write_text(
        '["__cached__"]',
        encoding="utf-8",
    )
    called = False

    def _boom(url: str, **_: Any) -> _FakeResp:  # 缓存命中不应触发网络
        nonlocal called
        called = True
        return _FakeResp({"rows": []})

    monkeypatch.setattr(httpx, "get", _boom)
    got = fetch_split("test", cache_file=cache)
    assert got == ["__cached__"]
    assert called is False


def test_fetch_split_writes_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / "rows.json"
    monkeypatch.setattr(
        httpx,
        "get",
        _fake_get_factory([{"rows": [{"row": _row("a")}]}]),
    )
    got = fetch_split("test", cache_file=cache)
    assert got[0]["instance_id"] == "a"
    assert cache.is_file()
    assert DATASET in cache.read_text(encoding="utf-8") or "a" in cache.read_text(encoding="utf-8")


# ---- main：CLI 入口 ----

def test_main_writes_tasks_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """mock fetch_split（绕网络），验证 CLI 生成 tasks.json 并打印摘要。"""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    out = tmp_path / "tasks.json"

    monkeypatch.setattr("kdagent.eval.swebench.fetch_split", lambda *a, **k: [_row("a"), _row("b")])
    rc = main(
        [
            "--repo", "joke2k/faker",
            "--repo-dir", str(repo_dir),
            "--out", str(out),
            "--run-id", "smoke",
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["run_id"] == "smoke"
    assert [t["instance_id"] for t in data["tasks"]] == ["a", "b"]


def test_main_empty_rejects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("kdagent.eval.swebench.fetch_split", lambda *a, **k: [])
    rc = main(
        [
            "--repo-dir", str(tmp_path),
            "--out", str(tmp_path / "t.json"),
        ]
    )
    assert rc == 2
