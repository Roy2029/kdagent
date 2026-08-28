"""07 §3.7 T9 `/metrics` 面板测试（D71）：渲染纯函数 + 命令接线 + Screen 装配。

渲染逻辑收敛在 `render_metrics_text`（纯函数单测）；Screen 装配用 Textual
`run_test()` headless 驱动（同 test_ui_evalreport 手法），数据用 Telemetry 真实落盘。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from conftest import FakeLLM

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.obs import Telemetry
from kdagent.obs.metrics import SessionMetrics, ToolStat
from kdagent.tools import build_default_registry
from kdagent.ui.app import KDApp
from kdagent.ui.commands import CommandContext, UIController, build_default_commands
from kdagent.ui.metricsscreen import MetricsScreen, render_metrics_text


def _sample(sid: str = "s1") -> SessionMetrics:
    """构造全口径聚合桶（渲染纯函数输入，非 JSONL 产物）。"""
    sm = SessionMetrics(session_id=sid, traces=2)
    sm.providers.add("deepseek-chat")
    sm.input_tokens, sm.output_tokens = 300, 150
    sm.cache_read_tokens, sm.cache_creation_tokens = 20, 10
    sm.llm_calls, sm.llm_errors, sm.llm_total_ms = 5, 1, 6000
    sm.llm_latencies_ms = [100, 200, 300, 400, 5000]
    sm.tools["ReadFile"] = ToolStat(calls=2, errors=1, total_ms=16)
    sm.tools["Grep"] = ToolStat(calls=1, errors=0, total_ms=5)
    sm.compact = {"force": 1, "auto": 2, "l2": 3}
    sm.permission = {"allow": 4, "deny": 1}
    sm.hook_runs = 5
    sm.cost_cny = 0.0026
    return sm


# ---- 渲染纯函数 -------------------------------------------------------------


def test_render_no_data_hints() -> None:
    text = render_metrics_text("s1", None, [])
    assert "暂无 trace 数据" in text and "s1" in text


def test_render_current_session_full_detail() -> None:
    text = render_metrics_text("s1", _sample(), [])
    assert "── 当前会话 s1 ──" in text
    assert "Trace 2 个" in text and "LLM 调用 5 次（错误 1）" in text
    assert "in 300" in text and "out 150" in text and "cache读 20" in text
    assert "¥0.0026" in text
    assert "avg 1200ms / p99 5000ms" in text
    assert "deepseek-chat" in text
    assert "ReadFile：2 次  成功率 50%  avg 8ms" in text
    assert "auto 2 / force 1 / l2 3" in text  # 按键名排序
    assert "allow 4 / deny 1" in text
    assert "Hook 运行" in text and "5 次" in text
    # 历史空段不出现
    assert "历史会话" not in text


def test_render_history_excludes_current_session() -> None:
    cur, other = _sample("s1"), _sample("s2")
    other.llm_calls, other.cost_cny = 3, 0.001
    text = render_metrics_text("s1", cur, [cur, other])
    assert "── 历史会话（1 个）──" in text
    assert "s2" in text and "llm 3" in text and "¥0.0010" in text
    # 当前会话不重复进历史计数（历史段只有 s2 的 llm 3）
    assert "llm 5" not in text.split("历史会话")[1]


def test_render_history_only_when_current_has_no_trace() -> None:
    text = render_metrics_text("s1", None, [_sample("s2")])
    assert "── 历史会话（1 个）──" in text and "s2" in text
    assert "当前会话 s1" not in text


# ---- /metrics 命令 handler --------------------------------------------------


class _MetricsUI:
    """记录 open_metrics 调用的 UIController 替身（其余方法空实现）。"""

    def __init__(self) -> None:
        self.opened = 0

    def add_system_message(self, text: str) -> None:
        self.messages = text

    def send_user_message(self, text: str) -> None: ...
    def set_plan_mode(self, enabled: bool) -> None: ...
    def is_plan_mode(self) -> bool:
        return False
    def get_token_count(self) -> int:
        return 0
    def get_context_tokens(self) -> int:
        return 0
    def refresh_status(self) -> None: ...
    def clear_chat(self) -> None: ...
    def request_exit(self) -> None: ...
    def set_active_session(self, session: object) -> None: ...
    def reload_config(self) -> str:
        return ""
    def set_permission_mode(self, mode: str) -> None: ...
    def get_permission_mode(self) -> str:
        return "default"
    def open_eval_report(self, run_id: str) -> None: ...
    def open_metrics(self) -> None:
        self.opened += 1


def _ctx(tmp_path: Path, ui: UIController) -> CommandContext:
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=FakeLLM([]),
        conversation=conv,
        tools=build_default_registry(),
        events=lambda e: None,
        work_dir=tmp_path,
    )
    return CommandContext(
        args="",
        agent=agent,
        conversation=conv,
        session=None,
        ui=ui,
        config=Config(),
        registry=build_default_commands(),
    )


def test_metrics_command_dispatches_open(tmp_path: Path) -> None:
    ui = _MetricsUI()
    build_default_commands().find("metrics").handler(_ctx(tmp_path, ui))  # type: ignore[union-attr]
    assert ui.opened == 1


# ---- Screen 装配（headless run_test + Telemetry 真实落盘） -------------------


def _emit_trace(obs_dir: Path, sid: str) -> None:
    telemetry = Telemetry(obs_dir)
    telemetry.begin_trace(sid, "metrics ui")
    with telemetry.span("llm.call", "client", {"model": "deepseek-chat"}) as s:
        if s is not None:
            s.attributes.update(input_tokens=100, output_tokens=50, cache_read_tokens=10)
    with telemetry.span("tool.exec", "tool", {"tool": "Grep", "is_error": False}):
        pass
    with telemetry.span("context.compact", "context", {"trigger": "auto"}):
        pass
    telemetry.end_trace()


def _body(screen: MetricsScreen) -> str:
    return str(screen.query_one("#metrics-body-static").content)


async def test_metrics_screen_renders_current_session(tmp_path: Path) -> None:
    work = tmp_path / "wk"
    obs = work / ".kdagent" / "obs"
    app = KDApp(
        config=Config(),
        llm=FakeLLM([]),
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=work,
        obs_dir=obs,
    )
    async with app.run_test() as pilot:
        _emit_trace(obs, app._session.id)  # 当前会话落盘
        app.dispatch_command("metrics", "")
        await pilot.pause()
        screen = cast(MetricsScreen, app.screen)
        body = _body(screen)
        assert isinstance(screen, MetricsScreen)
        assert f"Metrics · {app._session.id}" in str(screen.query_one("#metrics-header").content)
        assert "当前会话" in body and "LLM 调用 1 次（错误 0）" in body
        assert "Grep：1 次" in body and "auto 1" in body


async def test_metrics_screen_history_separate_session(tmp_path: Path) -> None:
    work = tmp_path / "wk"
    obs = work / ".kdagent" / "obs"
    app = KDApp(
        config=Config(),
        llm=FakeLLM([]),
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=work,
        obs_dir=obs,
    )
    async with app.run_test() as pilot:
        _emit_trace(obs, "s-other")  # 历史会话落盘（当前 session 无 trace）
        app.dispatch_command("metrics", "")
        await pilot.pause()
        screen = cast(MetricsScreen, app.screen)
        body = _body(screen)
        assert "当前会话" not in body
        assert "历史会话（1 个）" in body and "s-other" in body


async def test_metrics_obs_disabled_no_screen(tmp_path: Path) -> None:
    work = tmp_path / "wk"
    app = KDApp(
        config=Config(),
        llm=FakeLLM([]),
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=work,
    )
    async with app.run_test() as pilot:
        app.dispatch_command("metrics", "")
        await pilot.pause()
        assert not isinstance(app.screen, MetricsScreen)
