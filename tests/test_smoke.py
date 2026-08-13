"""M0 冒烟测试：包导入、版本、模块可见性、CLI。"""

from __future__ import annotations

import subprocess
import sys

import kdagent


def test_version_exported() -> None:
    assert kdagent.__version__ == "0.1.0"


def test_all_modules_importable() -> None:
    import kdagent.config
    import kdagent.context
    import kdagent.engine
    import kdagent.engine.llm
    import kdagent.eval
    import kdagent.harness
    import kdagent.hooks
    import kdagent.mcp
    import kdagent.memory
    import kdagent.obs
    import kdagent.permission
    import kdagent.sessions
    import kdagent.skill
    import kdagent.subagent
    import kdagent.tools
    import kdagent.ui

    assert kdagent.config.load_config().provider == "deepseek"


def test_cli_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kdagent", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "kdagent" in result.stdout
