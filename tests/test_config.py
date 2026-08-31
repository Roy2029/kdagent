"""config 加载测试（D98：load_config 支持 project_dir 参数）。

回归背景：`-d` 指定 work_dir 启动时，config 从进程 cwd 加载导致 work_dir 的
config 被忽略（kdagent-demo 的 `extra.max_tokens: 100000` 从未生效，进程一直
顶格 4096、长任务报「输出被 max_tokens 截断」）。此处验证 project_dir 根、
三源合并顺序与缺配置回退。

v052 review 增补（D5 散点闭合）：sessions.cleanup_days 配置与 getter、
hooks 列表三源追加合并去重、cli build_kdapp 启动接线 cleanup_expired。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kdagent.config import _hook_dedup_key, load_config
from kdagent.sessions.manager import SessionManager


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


# ---- 04 §3.6 会话保留：sessions.cleanup_days（D5 v052） ----


def test_cleanup_days_defaults_to_30(tmp_path: Path, isolated_home: Path) -> None:
    """零配置 → 默认 30 天过期清理。"""
    assert load_config(tmp_path).get_cleanup_days() == 30


def test_cleanup_days_reads_configured(tmp_path: Path, isolated_home: Path) -> None:
    """配置 `sessions.cleanup_days: 60` → 读回 60。"""
    kd_dir = tmp_path / ".kdagent"
    kd_dir.mkdir(parents=True, exist_ok=True)
    (kd_dir / "config.yaml").write_text(
        "sessions:\n  cleanup_days: 60\n", encoding="utf-8"
    )
    assert load_config(tmp_path).get_cleanup_days() == 60


def test_cleanup_days_zero_disables(tmp_path: Path, isolated_home: Path) -> None:
    """`cleanup_days: 0` = 关闭清理（cli 接线按 >0 判定）。"""
    kd_dir = tmp_path / ".kdagent"
    kd_dir.mkdir(parents=True, exist_ok=True)
    (kd_dir / "config.yaml").write_text(
        "sessions:\n  cleanup_days: 0\n", encoding="utf-8"
    )
    assert load_config(tmp_path).get_cleanup_days() == 0


def test_cleanup_days_invalid_falls_back(tmp_path: Path, isolated_home: Path) -> None:
    """非法值（非数字）回退默认 30（零配置可用）。"""
    kd_dir = tmp_path / ".kdagent"
    kd_dir.mkdir(parents=True, exist_ok=True)
    (kd_dir / "config.yaml").write_text(
        "sessions:\n  cleanup_days: '很多'\n", encoding="utf-8"
    )
    assert load_config(tmp_path).get_cleanup_days() == 30


# ---- hooks 列表三源追加合并（D5 v052：用户级与项目级 hook 应同时生效） ----


def _write_hooks(root: Path, filename: str, hooks_yaml: str) -> None:
    kd_dir = root / ".kdagent"
    kd_dir.mkdir(parents=True, exist_ok=True)
    (kd_dir / filename).write_text(f"hooks:\n{hooks_yaml}", encoding="utf-8")


def test_hooks_concat_across_sources(tmp_path: Path, isolated_home: Path) -> None:
    """用户级 + 项目级定义不同 hook → 追加合并（都保留），不再覆盖冲掉。"""
    _write_hooks(
        isolated_home,
        "config.yaml",
        "  - id: user-hook\n    event: pre_tool_use\n    action: {type: command, command: echo user}\n",
    )
    _write_hooks(
        tmp_path,
        "config.yaml",
        "  - id: proj-hook\n    event: pre_tool_use\n    action: {type: command, command: echo proj}\n",
    )
    hooks = load_config(tmp_path).hooks
    assert [h["id"] for h in hooks] == ["user-hook", "proj-hook"]


def test_hooks_same_behavior_dedup_project_wins(tmp_path: Path, isolated_home: Path) -> None:
    """用户级与项目级定义同行为 hook（同 event+if+action.type+command）→ 去重，后源覆盖前源。"""
    _write_hooks(
        isolated_home,
        "config.yaml",
        "  - id: user-hook\n    event: pre_tool_use\n    action: {type: command, command: echo same}\n",
    )
    _write_hooks(
        tmp_path,
        "config.yaml",
        "  - id: proj-hook\n    event: pre_tool_use\n    action: {type: command, command: echo same}\n",
    )
    hooks = load_config(tmp_path).hooks
    assert len(hooks) == 1
    assert hooks[0]["id"] == "proj-hook"  # 后源（项目级）优先


def test_hooks_other_list_keys_still_override(tmp_path: Path, isolated_home: Path) -> None:
    """非 hooks 的 list 键保持覆盖语义（D5 最小变更，防误伤）。"""
    kd_dir = isolated_home / ".kdagent"
    kd_dir.mkdir(parents=True, exist_ok=True)
    (kd_dir / "config.yaml").write_text("permissions:\n  tools: [a]\n", encoding="utf-8")
    kd_dir = tmp_path / ".kdagent"
    kd_dir.mkdir(parents=True, exist_ok=True)
    (kd_dir / "config.yaml").write_text("permissions:\n  tools: [b]\n", encoding="utf-8")
    config = load_config(tmp_path)
    assert config.permissions["tools"] == ["b"]  # 覆盖，不是 [a, b]


def test_hook_dedup_key_excludes_action_type(tmp_path: Path) -> None:
    """组合键：主负载取 command 而非 action.type 本身（type 是行为类别，非标识）。"""
    a = {"event": "pre_tool_use", "action": {"type": "command", "command": "echo x"}}
    b = {"event": "pre_tool_use", "action": {"type": "command", "command": "echo y"}}
    c = {"event": "pre_tool_use", "action": {"type": "prompt", "prompt": "提示"}}
    assert _hook_dedup_key(a) == _hook_dedup_key(a)
    assert _hook_dedup_key(a) != _hook_dedup_key(b)  # command 不同
    assert _hook_dedup_key(a) != _hook_dedup_key(c)  # action.type 不同


# ---- cli build_kdapp 启动接线 cleanup_expired（D5 v052） ----


def test_build_kdapp_cleans_expired_sessions(tmp_path: Path) -> None:
    """启动装配时清理过期会话（>30 天删除）；新建会话不受影响。

    真实走 cli.build_kdapp：配置默认 30 天，预置 31 天前的旧会话 → 装配后被删；
    装配本身（KDApp 构造）会新建一个会话，证明清理不阻断启动。
    """
    from kdagent.cli import build_kdapp

    mgr = SessionManager(tmp_path / ".kdagent" / "sessions")
    old = mgr.create()
    old.append_user("旧会话")
    # 篡改 ts 为 31 天前（对齐 test_sessions_manager.test_cleanup_expired 手法）
    line = old.file.read_text(encoding="utf-8").strip().splitlines()[0]
    rec = json.loads(line)
    rec["ts"] = int(time.time()) - 31 * 86400
    old.file.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

    build_kdapp(work_dir=tmp_path)

    assert not old.file.exists()  # 过期会话已清理
    sessions = SessionManager(tmp_path / ".kdagent" / "sessions").list()
    assert len(sessions) == 1  # 仅剩装配新建的当前会话
