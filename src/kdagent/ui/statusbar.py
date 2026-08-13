"""状态栏（规格 05 §3.2）：模式 / token / 工具数 / 工作目录 / 会话 id。

是 07 监控指标的最初形态：UsageEvent → token 实时更新。
"""

from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    """底部状态行，`update_status` 幂等更新。"""

    def update_status(
        self,
        *,
        mode: str,
        token_count: int,
        tool_count: int,
        work_dir: str,
        session_id: str | None = None,
    ) -> None:
        parts = [f"[{mode}] tokens: {token_count}", f"工具 {tool_count}", work_dir]
        if session_id:
            parts.append(session_id)
        self.update(" | ".join(parts))
