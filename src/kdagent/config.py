"""配置加载（用户级 / 项目级 / 本地级三源合并）。

M3-d 起 `load_config` 从 YAML 三源合并：用户级 `~/.kdagent/config.yaml`
→ 项目级 `{cwd}/.kdagent/config.yaml` → 本地级 `{cwd}/.kdagent/config.local.yaml`
（后源覆盖前源，dict 深合并）。文件不存在按默认值处理（零配置可用）。

M1 阶段：Config 为简单 dataclass；`load_api_key` 从环境变量 / `.env` 读取
DeepSeek key（live 测试与 TUI 启动共用）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(slots=True)
class Config:
    """KDAgent 运行时配置（M0 占位，字段随里程碑逐步扩充）。"""

    provider: str = "deepseek"
    kdagent_dir: str = ".kdagent"
    model: str = ""
    extra: dict[str, object] = field(default_factory=dict)
    # 07 可观测性（M2）：debug.log_full_prompt 全文日志开关；
    # otel.enabled 切 OTLP exporter（M2 仅接口）；obs.sanitize 出口脱敏规则。
    debug: dict[str, object] = field(default_factory=dict)
    otel: dict[str, object] = field(default_factory=dict)
    obs: dict[str, object] = field(default_factory=dict)
    # 06 M3 可控档：permissions.mode 默认权限模式（default/acceptEdits/plan/
    # bypassPermissions，/permissions 命令运行时切换）；hooks 是 hook 配置列表
    # （06 §3.10，HookEngine.load 的 `hooks:` 节）。
    permissions: dict[str, object] = field(default_factory=dict)
    hooks: list[object] = field(default_factory=list)
    # 09 M4-c 工具生态：mcp_servers 段（server 名 → {command, args, env}），
    # 三源合并 + 项目级覆盖用户级同名 Server（09 §3.2）。
    mcp_servers: dict[str, object] = field(default_factory=dict)
    # 10 M5-a SubAgent：agents 段（enable_verification_agent 开关，Verification 默认关
    # T27；后续可扩展默认模型/后台超时等）。
    agents: dict[str, object] = field(default_factory=dict)


def load_config() -> Config:
    """三源合并加载（用户级 → 项目级 → 本地级，后源覆盖前源）。"""
    data: dict[str, Any] = {}
    for path in (
        Path.home() / ".kdagent" / "config.yaml",
        Path.cwd() / ".kdagent" / "config.yaml",
        Path.cwd() / ".kdagent" / "config.local.yaml",
    ):
        data = _deep_merge(data, _load_yaml_dict(path))

    def _dict(key: str) -> dict[str, object]:
        v = data.get(key)
        return v if isinstance(v, dict) else {}

    def _list(key: str) -> list[object]:
        v = data.get(key)
        return v if isinstance(v, list) else []

    return Config(
        provider=str(data.get("provider", "deepseek")),
        kdagent_dir=str(data.get("kdagent_dir", ".kdagent")),
        model=str(data.get("model", "")),
        extra=_dict("extra"),
        debug=_dict("debug"),
        otel=_dict("otel"),
        obs=_dict("obs"),
        permissions=_dict("permissions"),
        hooks=_list("hooks"),
        mcp_servers=_dict("mcp_servers"),
        agents=_dict("agents"),
    )


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    """读 YAML 文件为 dict；文件缺失 / 解析失败 / 非 dict 一律空 dict（零配置可用）。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """dict 深合并：双方同层同 key 且都是 dict 时递归，否则后源覆盖。"""
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_api_key() -> str:
    """读取 DEEPSEEK_API_KEY：环境变量优先，回退项目根 `.env`。"""
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    for env_path in (Path.cwd() / ".env", _PROJECT_ROOT / ".env"):
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""
