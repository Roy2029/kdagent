"""状态栏（规格 05 §3.2）：模式 / token / 工具数 / 工作目录 / 会话 id。

是 07 监控指标的最初形态：UsageEvent → token 实时更新。
"""

from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    """底部状态行，`update_status` 幂等更新。

    05 §3.2：token 显示 `tokens: 45,230/200k`（当前窗口占用/窗口上限，07 指标最初形态）；
    `window_size` 传入时带上限，否则兼容纯 count。
    """

    def update_status(
        self,
        *,
        mode: str,
        token_count: int,
        tool_count: int,
        work_dir: str,
        session_id: str | None = None,
        window_size: int | None = None,
        permission: str = "",
    ) -> None:
        token_text = f"{token_count:,}"
        if window_size is not None:
            token_text = f"{token_text}/{window_size // 1000}k"
        parts = [f"[{mode}] tokens: {token_text}", f"工具 {tool_count}", work_dir]
        if permission:
            parts.append(f"权限 {permission}")
        if session_id:
            parts.append(session_id)
        self.update(" | ".join(parts))
