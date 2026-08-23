"""L1 危险命令黑名单（规格 06 §3.3）。

一组正则，命中直接 DENY，不经过任何规则/模式/确认。只对 Bash 生效——
文件工具有路径沙箱（L2）守护，这里守护的是命令通道。

按 shell 类型加载对应模式集：bash / powershell / cmd。本工具 Bash 经 Git Bash
执行（`shutil.which("bash")` 优先），故默认加载 bash 模式集；PowerShell/cmd
用户经 `permissions.blacklist_shell` 配置切换。
"""

from __future__ import annotations

import re

# (名称, 正则) 对——命中时 reason 报出命中的具体模式，便于定位。
# bash 模式集（规格 06 §3.3 表，正则按 bash 语法）。
_BASH: list[tuple[str, str]] = [
    ("递归删除根目录", r"\brm\s+(-[a-z]*[rf][a-z]*\s+)*/\s*$"),
    # WSL 挂载点删除：`/mnt/[a-z]` 是 Windows 盘符在 WSL 的视图，删除即真删 Windows
    # 文件。Bash 无路径沙箱（L2 不拦命令），黑名单兜底拦挂载点级删除——work_dir 内
    # 删除应走文件工具（有沙箱 + HITL），不走 Bash。`/mnt/[a-z]` 后须跟 `/`/空白/
    # 结尾/闭合引号，避免误伤 `/mnt/data` 这类多字母挂载点。
    # R2：`["']?` 容忍 Agent 给路径加引号（实测 `rm "/mnt/c/.../错误代码.txt"` 漏拦——
    # 引号破坏了 `rm ` 后直接跟 `/mnt/` 的匹配，黑名单形同虚设）；`\b` 防 `warm` 误伤。
    ("递归删除WSL挂载点", r"\brm\s+(-[a-z]*[rf][a-z]*\s+)*[\"']?/mnt/[a-z]([/\s\"']|$)"),
    # 盘符路径删除（Git Bash / Windows 语义）：`D:\` 或 `D:/` 开头，同样容忍引号。
    ("递归删除Windows盘符路径", r"\brm\s+(-[a-z]*[rf][a-z]*\s+)*[\"']?[A-Za-z]:[\\/]"),
    ("格式化磁盘", r"mkfs\."),
    ("直接写磁盘设备", r"dd\s+if=.*of=/dev/"),
    ("递归改根目录权限", r"chmod\s+-R\s+777\s+(/|\./)?$"),
    ("fork bomb", r":\(\)\{\s*:\s*\|\s*:\s*&\s*\};:"),
    ("管道执行远程脚本(curl)", r"curl\s+.*\|\s*(ba|pw)?sh"),
    ("管道执行远程脚本(wget)", r"wget\s+.*\|\s*(ba|pw)?sh"),
    ("覆盖磁盘设备", r">\s*/dev/sd"),
]

# powershell 模式集（Windows 默认 shell；Remove-Item 递归强删 / 卷级格式化 / iex 远程执行）。
_POWERSHELL: list[tuple[str, str]] = [
    ("递归强制删除根目录", r"Remove-Item\s+.*(-Recurse|-Force).*[A-Za-z]:\\\s*$"),
    ("卷级格式化", r"(Format-Volume|Initialize-Disk|Clear-Disk|format)\b"),
    ("远程脚本执行(iex)", r"(Invoke-WebRequest|iwr|curl|wget)\s+.*\|\s*(iex|Invoke-Expression)"),
    ("删除系统目录", r"Remove-Item\s+.*Windows\\System32"),
]

# cmd 模式集。
_CMD: list[tuple[str, str]] = [
    ("递归删除根目录", r"(del|erase)\s+(/[a-z]+\s+)+[A-Za-z]:\\\s*$"),
    ("递归删除目录树", r"rd\s+(/[a-z]+\s+)+[A-Za-z]:\\\s*$"),
    ("卷级格式化", r"(format\s+[A-Za-z]:|diskpart)"),
    ("远程脚本执行", r"(curl|certutil\s+-decode)\s+.*\|\s*cmd"),
]

_SETS: dict[str, list[tuple[str, str]]] = {
    "bash": _BASH,
    "powershell": _POWERSHELL,
    "cmd": _CMD,
}


class CommandBlacklist:
    """危险命令黑名单。`match` 命中返回模式名（进 DENY reason），未命中返回 None。"""

    def __init__(self, shell: str = "bash") -> None:
        if shell not in _SETS:
            raise ValueError(f"未知 shell 类型：{shell}（可选：{', '.join(_SETS)}）")
        self._shell = shell
        self._patterns = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in _SETS[shell]]

    @property
    def shell(self) -> str:
        return self._shell

    def match(self, command: str) -> str | None:
        """命中返回模式名（用于 DENY reason），未命中 None。"""
        for name, pattern in self._patterns:
            if pattern.search(command):
                return name
        return None

    def __len__(self) -> int:
        return len(self._patterns)
