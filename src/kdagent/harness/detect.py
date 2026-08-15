"""测试基建探测（规格 12 §3.1 激活条件 2）。

项目有测试基建 → 启动时注入「此项目有测试，改代码后应自测」提示（T32 提示词引导，
非强制门禁）。探测一次（启动时），结果作为 system-reminder 拼入 system——低频
变化不影响前缀缓存（与 Skill 清单注入同机制，见 09 §3.9）。
"""

from __future__ import annotations

from pathlib import Path

# 探测目标：pytest 配置 / 测试文件 / jest 配置 / package.json scripts.test
_CONFIG_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pytest.ini", ()),
    ("pyproject.toml", ("[tool.pytest",)),
    ("setup.cfg", ("[tool:pytest]",)),
    ("package.json", ('"test"',)),
    ("jest.config.js", ()),
    ("jest.config.ts", ()),
    ("vitest.config.ts", ()),
)
# 测试文件 glob（限深度 2，避免大项目全量扫慢）
_TEST_GLOBS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "tests/*.py",
    "*/test_*.py",
    "*/tests/*.py",
)


def detect_test_infra(work_dir: Path) -> str | None:
    """探测项目测试基建 → 提示文本；无测试基建返回 None（不注入噪音）。"""
    hints: list[str] = []
    for filename, markers in _CONFIG_HINTS:
        path = work_dir / filename
        if not path.is_file():
            continue
        if not markers:
            hints.append(filename)
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(marker in text for marker in markers):
            hints.append(filename)
    test_files: list[str] = []
    for pattern in _TEST_GLOBS:
        for p in work_dir.glob(pattern):
            if p.is_file():
                test_files.append(str(p.relative_to(work_dir)))
                break
        if len(test_files) >= 3:
            break
    hints.extend(test_files)
    if not hints:
        return None
    shown = "、".join(hints[:4])
    if len(hints) > 4:
        shown += f" 等 {len(hints)} 项"
    return (
        "<system-reminder>\n检测到此项目含测试基建（" + shown
        + "）。修改代码后应运行测试自测（TestRunner 工具）；"
        + "测试失败必须基于失败信息修复后重跑，不得绕开测试或伪造通过。\n</system-reminder>"
    )
