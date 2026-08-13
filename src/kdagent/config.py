"""配置加载（用户级 / 项目级 / 本地级三源合并）。

M0 阶段仅占位——具体 schema、三源合并与覆盖规则随 M1 落地。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Config:
    """KDAgent 运行时配置（M0 占位，字段随里程碑逐步扩充）。"""

    provider: str = "deepseek"
    kdagent_dir: str = ".kdagent"
    model: str = ""
    extra: dict[str, object] = field(default_factory=dict)


def load_config() -> Config:
    """从三源（用户级 / 项目级 / 本地级）合并加载配置。M0 返回默认值。"""
    return Config()
