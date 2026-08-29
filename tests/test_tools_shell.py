"""Bash 工具测试（规格 03 §3.5：stdout / stderr / exit code）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import kdagent.tools.shell as shell_mod
from kdagent.config import Config
from kdagent.tools.base import ToolContext
from kdagent.tools.shell import Bash, wsl_delete_diagnosis


def _ctx(work_dir: Path) -> ToolContext:
    return ToolContext(work_dir=work_dir, config=Config(), tool_use_id="bash_1")


@pytest.mark.skipif(shutil.which("bash") is None, reason="无 bash")
async def test_bash_echo_stdout(tmp_path: Path) -> None:
    result = await Bash().execute(_ctx(tmp_path), {"command": "echo hello"})
    assert result.is_error is False
    assert "hello" in result.content
    assert "[exit] 0" in result.content


async def test_bash_nonzero_exit_is_error(tmp_path: Path) -> None:
    result = await Bash().execute(_ctx(tmp_path), {"command": "exit 3"})
    assert result.is_error is True
    assert "[exit] 3" in result.content


async def test_bash_cwd_is_work_dir(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    result = await Bash().execute(_ctx(tmp_path), {"command": "ls"})
    assert result.is_error is False
    assert "marker.txt" in result.content


async def test_bash_empty_command_rejected(tmp_path: Path) -> None:
    errors = Bash().validate_input({"command": "   "})
    assert errors and "command" in errors[0]


def test_bash_meta_declarations() -> None:
    tool = Bash()
    assert tool.require_confirm is True
    assert tool.is_destructive() is True  # 保守声明（D10）
    assert tool.is_concurrency_safe({}) is False  # 保守声明
    assert tool.is_read_only() is False


# ---- R3：WSL 删除映射诊断（方案 B，纯函数） ----

def test_wsl_diagnosis_detects_drive_target() -> None:
    """WSL bash + Windows 盘符 rm 目标 → 命中，给出 /mnt/d 转换路径。"""
    d = wsl_delete_diagnosis('rm -f D:\\old-build', "C:\\Windows\\System32\\bash.exe")
    assert d is not None
    assert "映射诊断" in d
    assert "/mnt/d/old-build" in d


def test_wsl_diagnosis_unix_bash() -> None:
    """WSL 内嵌 Linux bash（/usr/bin/bash）同样命中。"""
    d = wsl_delete_diagnosis("rm -rf D:/Projects/old", "/usr/bin/bash")
    assert d is not None
    assert "/mnt/d/Projects/old" in d


def test_wsl_diagnosis_git_bash_skipped() -> None:
    """Git Bash（盘符路径可见）→ 不诊断（避免误报）。"""
    d = wsl_delete_diagnosis("rm -rf D:\\old-build", "C:\\Program Files\\Git\\bin\\bash.exe")
    assert d is None


def test_wsl_diagnosis_no_bash_skipped() -> None:
    """无 bash 可定位 → 不诊断。"""
    assert wsl_delete_diagnosis("rm -rf D:\\x", None) is None


def test_wsl_diagnosis_non_drive_target_skipped() -> None:
    """非盘符路径（Linux 路径）→ 不命中。"""
    d = wsl_delete_diagnosis("rm -rf /tmp/build", "/usr/bin/bash")
    assert d is None


def test_wsl_diagnosis_non_rm_skipped() -> None:
    """非 rm 命令 → 不命中（避免无关诊断）。"""
    d = wsl_delete_diagnosis("ls -la D:\\x", "/usr/bin/bash")
    assert d is None


# ---- UTF-16 解码 + WSL 冷启动重试（2026-08-28 d17c 会话实测） ----

def test_decode_output_utf8() -> None:
    """普通 UTF-8 文本原样。"""
    assert shell_mod._decode_output("你好 hello".encode()) == "你好 hello"


def test_decode_output_utf16le_with_bom() -> None:
    """WSL 启动器带 BOM 的 UTF-16LE 错误信息 → 可读文本（不再是 \\u0000 乱码）。"""
    raw = "错误码: Bash/Service/E_UNEXPECTED".encode("utf-16")
    assert shell_mod._decode_output(raw) == "错误码: Bash/Service/E_UNEXPECTED"


def test_decode_output_utf16le_without_bom() -> None:
    """无 BOM 的 UTF-16LE（空字节启发式命中）。"""
    raw = "Error code: Bash/Service/E_UNEXPECTED".encode("utf-16-le")
    assert shell_mod._decode_output(raw) == "Error code: Bash/Service/E_UNEXPECTED"


def test_decode_output_ascii_not_misdetected() -> None:
    """普通 ASCII 输出无空字节 → 不误判 UTF-16。"""
    assert shell_mod._decode_output(b"ls -la") == "ls -la"


def test_decode_output_empty() -> None:
    assert shell_mod._decode_output(b"") == ""


def test_wsl_launcher_failure_detected() -> None:
    """E_UNEXPECTED / Bash/Service 签名命中。"""
    assert shell_mod._is_wsl_launcher_failure("错误码: Bash/Service/E_UNEXPECTED") is True
    assert shell_mod._is_wsl_launcher_failure("E_UNEXPECTED") is True


def test_wsl_launcher_failure_not_detected() -> None:
    assert shell_mod._is_wsl_launcher_failure("hello") is False


async def test_bash_retries_on_wsl_cold_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WSL 启动器冷启动 E_UNEXPECTED → 短等后重试，第二次成功。"""
    calls: list[str] = []
    fail = "错误码: Bash/Service/E_UNEXPECTED".encode("utf-16")

    async def fake_run(bash: str | None, command: str, cwd: str):
        calls.append(command)
        if len(calls) == 1:
            return 1, fail, b""
        return 0, b"hello", b""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(
        shell_mod.shutil, "which", lambda name: r"C:\Windows\System32\bash.exe"
    )
    monkeypatch.setattr(shell_mod, "_run_command", fake_run)
    monkeypatch.setattr(shell_mod, "_sleep", fake_sleep)

    result = await Bash().execute(_ctx(tmp_path), {"command": "ls -la"})
    assert result.is_error is False
    assert "hello" in result.content
    assert "[exit] 0" in result.content
    assert "WSL 冷启动后恢复" in result.content
    assert len(calls) == 2


async def test_bash_retry_exhausts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """重试耗尽 → 返回最后一次错误，且 E_UNEXPECTED 以可读文本出现（非乱码）。"""
    fail = "错误码: Bash/Service/E_UNEXPECTED".encode("utf-16")
    calls: list[str] = []

    async def fake_run(bash: str | None, command: str, cwd: str):
        calls.append(command)
        return 1, fail, b""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(
        shell_mod.shutil, "which", lambda name: r"C:\Windows\System32\bash.exe"
    )
    monkeypatch.setattr(shell_mod, "_run_command", fake_run)
    monkeypatch.setattr(shell_mod, "_sleep", fake_sleep)

    result = await Bash().execute(_ctx(tmp_path), {"command": "ls"})
    assert result.is_error is True
    assert "E_UNEXPECTED" in result.content
    assert len(calls) == 1 + shell_mod._WSL_RETRIES


async def test_bash_no_retry_for_non_wsl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Git Bash（非 WSL 启动器）不重试，普通失败原样返回。"""
    calls: list[str] = []

    async def fake_run(bash: str | None, command: str, cwd: str):
        calls.append(command)
        return 1, b"some error", b""

    monkeypatch.setattr(
        shell_mod.shutil, "which", lambda name: r"C:\Program Files\Git\bin\bash.exe"
    )
    monkeypatch.setattr(shell_mod, "_run_command", fake_run)

    result = await Bash().execute(_ctx(tmp_path), {"command": "exit 3"})
    assert result.is_error is True
    assert "some error" in result.content
    assert len(calls) == 1
