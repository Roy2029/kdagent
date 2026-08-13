"""KDAgent 命令行入口。

M0：仅 `--version`；M1 起接入 Textual TUI（规格 05）。
"""

from __future__ import annotations

import argparse
import sys

from kdagent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kdagent",
        description="KDAgent - 类 Claude Code 的 Coding Agent",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kdagent {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    build_parser().parse_args(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
