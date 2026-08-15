"""记忆存储（08 §3.2）：两类记忆目录 + MEMORY.md 索引维护。

文件是唯一真相来源（§3.1）：提取写入、主动读写、治理整理都围绕 `memory/*.md`
这一层。MEMORY.md 是汇合点——治理修剪它、静默读注入它。

**写入边界**：所有操作路径被限定在 memory 根目录内（08 §3.9：复用 06 路径沙箱
的语义——本层直接做 `resolve` 前缀校验，防 name 穿越出目录）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kdagent.memory.model import (
    INDEX_MAX_BYTES,
    INDEX_MAX_LINES,
    MemoryFile,
    MemoryType,
    default_index_line,
    parse_memory,
    serialize_memory,
)

# 分类 → 目录作用域（08 §3.2 表）。
_USER_TYPES: frozenset[str] = frozenset({"user", "feedback"})
_PROJECT_TYPES: frozenset[str] = frozenset({"project", "reference"})

_INDEX_FILENAME = "MEMORY.md"


@dataclass(frozen=True, slots=True)
class ApplyReport:
    """一次 apply_ops 的执行结果（诊断/测试断言用）。"""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.deleted)


class MemoryStore:
    """跨用户级/项目级两个 memory 根的记忆仓库。"""

    def __init__(self, user_root: Path, project_root: Path) -> None:
        self._user_root = user_root.resolve()
        self._project_root = project_root.resolve()
        self._cache: list[MemoryFile] | None = None  # 懒加载；写操作后失效

    # ---- 目录与清单 ----

    @property
    def user_root(self) -> Path:
        return self._user_root

    @property
    def project_root(self) -> Path:
        return self._project_root

    def ensure(self) -> None:
        """建 memory 目录（幂等）；两 scope 各含 MEMORY.md 索引。"""
        for root in (self._user_root, self._project_root):
            root.mkdir(parents=True, exist_ok=True)
            (root / _INDEX_FILENAME).touch(exist_ok=True)

    def list_all(self) -> list[MemoryFile]:
        """全部记忆（用户级 + 项目级），按 name 排序。懒加载并缓存。"""
        if self._cache is None:
            files: list[MemoryFile] = []
            for root in (self._user_root, self._project_root):
                for path in sorted(root.glob("*.md")):
                    if path.name == _INDEX_FILENAME:
                        continue
                    f = parse_memory(path.read_text(encoding="utf-8"), fallback_name=path.stem)
                    if f is not None:
                        files.append(f)
            self._cache = files
        return list(self._cache)

    def read(self, name: str) -> MemoryFile | None:
        for f in self.list_all():
            if f.name == name:
                return f
        return None

    # ---- 写操作（均更新 MEMORY.md 索引） ----

    def create(self, f: MemoryFile) -> bool:
        """创建新记忆；name 已存在 → False（不覆盖）。"""
        if self.read(f.name) is not None:
            return False
        return self._write(f)

    def update(self, f: MemoryFile) -> bool:
        """更新已有记忆（覆写文件 + 更新索引行）；不存在 → False。"""
        if self.read(f.name) is None:
            return False
        return self._write(f)

    def delete(self, name: str) -> bool:
        """删除记忆：删文件 + 从索引移除指针；不存在 → False。"""
        f = self.read(name)
        if f is None:
            return False
        root = self._scope_root(f.type)
        (root / f"{f.name}.md").unlink(missing_ok=True)
        self._remove_index_line(root, f.index_line or default_index_line(f))
        self._cache = None
        return True

    def apply_ops(self, ops: list[dict[str, object]]) -> ApplyReport:
        """执行提取/治理产出的操作集（create/update/delete）；越界名跳过。"""
        report = ApplyReport()
        for op in ops:
            action = str(op.get("action", ""))
            name = str(op.get("name", "")).strip()
            if not name or not self._safe_name(name):
                report.skipped.append(name or "<空>")
                continue
            if action == "delete":
                if self.delete(name):
                    report.deleted.append(name)
                else:
                    report.skipped.append(name)
                continue
            mf = self._op_to_memory(op, name)
            if mf is None:
                report.skipped.append(name)
                continue
            if action == "create" and self.create(mf):
                report.created.append(name)
            elif action == "update" and self.update(mf):
                report.updated.append(name)
            else:
                report.skipped.append(name)
        return report

    # ---- 注入 ----

    def index_markdown(self) -> str:
        """注入上下文用：两个 MEMORY.md 索引拼接（08 §3.3 静默读）。"""
        parts: list[str] = []
        for root in (self._user_root, self._project_root):
            idx = root / _INDEX_FILENAME
            if idx.is_file():
                text = idx.read_text(encoding="utf-8").strip()
                if text:
                    parts.append(text)
        if not parts:
            return ""
        return "## MEMORY.md 记忆索引\n" + "\n".join(parts) + "\n"

    # ---- 内部 ----

    def _scope_root(self, type_: MemoryType) -> Path:
        if type_ in _PROJECT_TYPES:
            return self._project_root
        return self._user_root

    def _safe_name(self, name: str) -> bool:
        """name 必须落在 memory 根内：解析后仍在前缀下（防 `../` 穿越）。"""
        for root in (self._user_root, self._project_root):
            candidate = (root / f"{name}.md").resolve()
            if candidate.is_relative_to(root):
                return True
        return False

    def _write(self, f: MemoryFile) -> bool:
        root = self._scope_root(f.type)
        self.ensure()
        path = root / f"{f.name}.md"
        if not path.resolve().is_relative_to(root):
            return False
        path.write_text(serialize_memory(f), encoding="utf-8")
        self._append_index_line(root, f.index_line or default_index_line(f))
        self._cache = None
        return True

    def _op_to_memory(self, op: dict[str, object], name: str) -> MemoryFile | None:
        raw_type = str(op.get("type", ""))
        if raw_type not in ("user", "feedback", "project", "reference"):
            return None
        return MemoryFile(
            name=name,
            description=str(op.get("description", "")).strip(),
            type=raw_type,  # type: ignore[arg-type]
            content=str(op.get("content", "")),
            index_line=str(op.get("index_line", "")).strip(),
        )

    def _index_path(self, root: Path) -> Path:
        return root / _INDEX_FILENAME

    def _append_index_line(self, root: Path, line: str) -> None:
        """追加索引行（去重），并维持 200 行/25KB 上限（超限截断末尾）。"""
        lines = [ln for ln in self._read_index_lines(root) if ln.strip() != line.strip()]
        lines.append(line)
        self._write_index(root, lines)

    def _remove_index_line(self, root: Path, line: str) -> None:
        lines = [ln for ln in self._read_index_lines(root) if ln.strip() != line.strip()]
        self._write_index(root, lines)

    def _read_index_lines(self, root: Path) -> list[str]:
        path = self._index_path(root)
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def _write_index(self, root: Path, lines: list[str]) -> None:
        """截断到 200 行/25KB 上限（保留最新，超限丢最旧）。"""
        lines = lines[-INDEX_MAX_LINES:]
        text = "\n".join(lines) + "\n" if lines else ""
        if len(text.encode("utf-8")) > INDEX_MAX_BYTES:
            # 按行砍到字节上限以内（最少保留 1 行）。
            kept: list[str] = []
            total = 0
            for line in reversed(lines):
                total += len((line + "\n").encode("utf-8"))
                if total > INDEX_MAX_BYTES:
                    break
                kept.append(line)
            text = "\n".join(reversed(kept)) + "\n" if kept else ""
        self._index_path(root).write_text(text, encoding="utf-8")


def build_memory_store(work_dir: Path, *, kdagent_dir: str = ".kdagent") -> MemoryStore:
    """装配：用户级 `~/.kdagent/memory/` + 项目级 `{work_dir}/.kdagent/memory/`。"""
    user_root = Path.home() / ".kdagent" / "memory"
    project_root = work_dir / kdagent_dir / "memory"
    return MemoryStore(user_root, project_root)
