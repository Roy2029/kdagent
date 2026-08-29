"""config 加载测试（D98：load_config 支持 project_dir 参数）。

回归背景：`-d` 指定 work_dir 启动时，config 从进程 cwd 加载导致 work_dir 的
config 被忽略（kdagent-demo 的 `extra.max_tokens: 100000` 从未生效，进程一直
顶格 4096、长任务报「输出被 max_tokens 截断」）。此处验证 project_dir 根、
三源合并顺序与缺配置回退。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdagent.config import load_config


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离用户级配置：假 home 指向 tmp_path/home，避免读到真实 ~/.kdagent/config.yaml。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _write(root: Path, filename: str, max_tokens: int) -> None:
    """在 root/.kdagent/{filename} 写带 max_tokens 的配置。"""
    kd_dir = root / ".kdagent"
    kd_dir.mkdir(parents=True, exist_ok=True)
    (kd_dir / filename).write_text(
        f"extra:\n  max_tokens: {max_tokens}\n", encoding="utf-8"
    )


def test_load_config_reads_project_dir(tmp_path: Path, isolated_home: Path) -> None:
    """project_dir 指定时从该目录读配置（D98 核心回归）。"""
    _write(tmp_path, "config.yaml", 8000)
    assert load_config(tmp_path).extra.get("max_tokens") == 8000


def test_load_config_defaults_to_cwd(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project_dir 缺省回退 cwd（原行为不回归）。"""
    _write(tmp_path, "config.yaml", 8000)
    monkeypatch.chdir(tmp_path)
    assert load_config().extra.get("max_tokens") == 8000


def test_project_overrides_user(
    tmp_path: Path, isolated_home: Path
) -> None:
    """三源合并：用户级 → 项目级，后源覆盖前源。"""
    _write(isolated_home, "config.yaml", 2000)
    _write(tmp_path, "config.yaml", 8000)
    assert load_config(tmp_path).extra.get("max_tokens") == 8000


def test_local_overrides_project(tmp_path: Path, isolated_home: Path) -> None:
    """三源合并：本地级 config.local.yaml 覆盖项目级。"""
    _write(tmp_path, "config.yaml", 8000)
    _write(tmp_path, "config.local.yaml", 12000)
    assert load_config(tmp_path).extra.get("max_tokens") == 12000


def test_load_config_empty_root_defaults(tmp_path: Path, isolated_home: Path) -> None:
    """无任何配置时回退默认：max_tokens 缺省（agent 侧回退 100000），provider 默认。"""
    config = load_config(tmp_path)
    assert config.extra.get("max_tokens") is None
    assert config.provider == "deepseek"
