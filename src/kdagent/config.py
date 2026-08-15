"""配置加载（用户级 / 项目级 / 本地级三源合并）。

M1 阶段：Config 为简单 dataclass；`load_api_key` 从环境变量 / `.env` 读取
DeepSeek key（live 测试与 TUI 启动共用）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


def load_config() -> Config:
    """从三源（用户级 / 项目级 / 本地级）合并加载配置。M1 返回默认值。"""
    return Config()


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
