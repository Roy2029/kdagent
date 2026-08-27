"""L2 路径沙箱（规格 06 §3.4）。

防模型被诱导读写项目之外，以及 **symlink/junction 逃逸**——目录内建链接指到项目外，
字面路径检查会被骗过。`Path.resolve()` 在所有平台解析符号链接（Windows 含 junction）。

允许目录：项目根（work_dir）+ 系统临时目录 + 配置白名单。Windows 路径大小写不敏感
（比较前 casefold）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_WINDOWS = os.name == "nt"


def _resolve(path: Path) -> Path:
    """解析符号链接/junction；文件不存在（新建场景）时退化为父目录解析 + 基名。"""
    try:
        return path.resolve(strict=False)
    except OSError:
        # resolve(strict=False) 对极深不存在路径仍可能抛；兜底父目录解析。
        try:
            return path.parent.resolve(strict=False) / path.name
        except OSError:
            return path.absolute()


class PathSandbox:
    """判断一个请求路径是否落在允许根内（解析符号链接后）。"""

    def __init__(
        self,
        allowed_roots: list[Path],
        work_dir: Path | None = None,
        *,
        include_tempdir: bool = True,
    ) -> None:
        self._work_dir = Path.cwd() if work_dir is None else work_dir
        roots: list[Path] = [*allowed_roots]
        # 项目根（work_dir）总是允许目录（docstring 既有设计）——此前漏加导致
        # 项目内文件全被 L2 拦成 HITL（M1 实测：项目记忆 ReadFile 判 ask）。
        # 相对路径以 work_dir 为基准归一；系统临时目录总是放行（L1 落盘等中间产物）。
        # include_tempdir=False 供测试隔离（tmp_path 本身就在系统临时目录下）。
        roots.append(self._work_dir)
        if include_tempdir:
            roots.append(Path(tempfile.gettempdir()))
        self._roots = [str(_resolve(r)) for r in roots]

    def contains(self, requested: str | Path) -> bool:
        """路径（解析后）是否在任一允许根内。"""
        p = Path(requested)
        if not p.is_absolute():
            p = self._work_dir / p
        real = _resolve(p)
        real_str = str(real).casefold() if _WINDOWS else str(real)
        for root in self._roots:
            root_str = root.casefold() if _WINDOWS else root
            sep = os.sep
            if real_str == root_str or real_str.startswith(root_str + sep):
                return True
        return False
