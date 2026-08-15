"""WorktreeManager：Git Worktree 空间隔离（规格 10 §3.10-3.13，M5-b 核心）。

分支是时间隔离（同一时刻一个工作目录、切分支刷 mtime 引发全量重建）；
Worktree 是空间隔离——同一仓库多个独立工作目录、共享 `.git`、历史统一：

    ./project/            → main 分支（主 Agent）
    ./project/.kdagent/worktrees/agent-3f2b1c0/   → worktree-agent-3f2b1c0 分支（子 Agent）

生命周期（§3.11）：创建六步（验证 → 查重 → 路径/分支名 → `git worktree add -B`
→ 记录持久化）→ 使用（explicit cwd，工具显式传 worktree 路径，无全局 chdir）
→ 退出（变更保护 fail-closed → 可选 remove）→ 过期清理漏斗（临时命名 + 过期 +
fail-closed，孤儿安全网）。

Windows 约束（§3.13）：`git worktree` 跨平台（Git for Windows 2.5+）；所有 git
子进程设 `GIT_TERMINAL_PROMPT=0` + `GIT_ASKPASS=""` + stdin ignore 三重防挂起。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# 临时命名白名单：子 Agent/workflow 自动清理对象；用户 `/worktree create my-feature`
# 不匹配 `agent-` 前缀，永不清（§3.11 过期清理漏斗第一道）。
DEFAULT_TEMP_PREFIX = "agent-"

# Slug 白名单（防路径遍历，LLM 输入不可信）：字母数字 + `._-`；`/` 作嵌套分隔符
# 分段校验；拒绝 `.` 开头（隐藏目录 / `..` 穿越）。
_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_MAX_NAME_LEN = 64

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


class WorktreeError(Exception):
    """worktree 生命周期异常（验证失败 / 变更保护拒绝 / git 调用失败）。"""


@dataclass(frozen=True, slots=True)
class Worktree:
    """一个已创建的 worktree 记录（持久化到 worktree_session.json）。"""

    name: str
    path: str  # 绝对路径（显式 cwd 模式：工具每次调用显式取此路径）
    branch: str
    based_on: str  # 创建时的基点（默认 HEAD）
    head_commit: str  # 创建时 HEAD SHA（「无新 commit」判定的基准）
    created: float  # time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Worktree:
        return cls(
            name=str(d["name"]),
            path=str(d["path"]),
            branch=str(d["branch"]),
            based_on=str(d.get("based_on", "HEAD")),
            head_commit=str(d.get("head_commit", "")),
            created=float(d.get("created", 0.0)),
        )


def validate_name(name: str) -> None:
    """Slug 安全验证（§3.11）：白名单 + 长度限制 + 嵌套分段校验。

    非法时抛 WorktreeError；合法返回 None。`..`（`..` 单段）、`.hidden`、空名、超长
    均拒绝——防路径穿越与隐藏目录，LLM 输入不可信。
    """
    if not name or len(name) > _MAX_NAME_LEN:
        raise WorktreeError(f"worktree 名长度必须在 1-{_MAX_NAME_LEN}：{name!r}")
    for segment in name.split("/"):
        if not segment or segment.startswith(".") or segment == "..":
            raise WorktreeError(f"worktree 名含非法段：{name!r}")
        if not set(segment) <= _SLUG_CHARS:
            raise WorktreeError(
                f"worktree 名只允许字母数字与 ._-（/ 作嵌套分隔）：{name!r}"
            )


class WorktreeManager:
    """worktree 生命周期管理（§3.11）。单进程内查重；持久化便于跨会话恢复。"""

    def __init__(
        self,
        repo_root: Path,
        worktree_dir: Path | None = None,
        *,
        max_age: float = 3600.0,
        temp_prefix: str = DEFAULT_TEMP_PREFIX,
        symlink_directories: tuple[str, ...] = (),
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.worktree_dir = (
            Path(worktree_dir).resolve()
            if worktree_dir is not None
            else self.repo_root / ".kdagent" / "worktrees"
        )
        self.max_age = max_age  # 过期清理：临时命名 + 超过此秒数才进入候选
        self.temp_prefix = temp_prefix
        # 创建后设置 C（10 §3.11）：软链大依赖目录（node_modules/.venv）到 worktree。
        # Windows 软链需管理员/开发者模式 → best-effort，失败仅警告不中断。
        self.symlink_directories = symlink_directories
        self._session_file = self.worktree_dir / "worktree_session.json"
        self._active: dict[str, Worktree] = {}
        self._load()

    # ---- 创建（§3.11 六步） ----

    def create(self, name: str, based_on: str = "HEAD") -> Worktree:
        """创建六步：验证 → 查重 → 路径/分支名 → git worktree add -B → 记录持久化。"""
        validate_name(name)
        if name in self._active:
            raise WorktreeError(f"worktree 已存在：{name}（{self._active[name].path}）")
        self.worktree_dir.mkdir(parents=True, exist_ok=True)
        path = self.worktree_dir / name
        if path.exists():
            raise WorktreeError(f"worktree 目录已占用：{path}")
        branch = f"worktree-{name.replace('/', '+')}"
        head_commit = self._git("rev-parse", "--verify", "HEAD").strip()
        self._git(
            "worktree",
            "add",
            "-B",
            branch,
            str(path),
            based_on,
        )
        wt = Worktree(
            name=name,
            path=str(path),
            branch=branch,
            based_on=based_on,
            head_commit=head_commit,
            created=time.time(),
        )
        self._active[name] = wt
        self._apply_post_create(path)
        self._save()
        return wt

    # ---- 创建后设置（§3.11，只对新建） ----

    def _apply_post_create(self, wt_path: Path) -> None:
        """A 复制被忽略但需要的文件（.worktreeinclude 声明）+ C 软链大依赖目录。

        best-effort：任何一项失败不阻断 worktree 使用（隔离执行不受影响）。
        `.worktreeinclude` 语义 = gitignore 语法的路径清单（.env 最典型）；复制的
        文件必须是主仓库已 ignore 的，否则会让 worktree 永久「有变更」（status 脏）。
        """
        self._copy_included(wt_path)
        for rel in self.symlink_directories:
            src = self.repo_root / rel
            dst = wt_path / rel
            if not src.is_dir() or dst.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(src, dst, target_is_directory=True)
            except OSError:
                continue  # Windows 权限不足 → best-effort 跳过

    def _copy_included(self, wt_path: Path) -> None:
        inc = self.repo_root / ".worktreeinclude"
        if not inc.is_file():
            return
        for raw in inc.read_text(encoding="utf-8").splitlines():
            rel = raw.strip()
            if not rel or rel.startswith("#"):
                continue
            src = self.repo_root / rel
            dst = wt_path / rel
            if not src.exists() or dst.exists():
                continue
            try:
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            except OSError:
                continue

    # ---- 查询 ----

    def get(self, name: str) -> Worktree | None:
        return self._active.get(name)

    def list(self) -> list[Worktree]:
        return sorted(self._active.values(), key=lambda w: w.created)

    def path(self, name: str) -> Path | None:
        wt = self._active.get(name)
        return Path(wt.path) if wt else None

    # ---- 变更检测（fail-closed 判定基准） ----

    def has_changes(self, name: str) -> bool:
        """有变更 = `git status --porcelain` 非空 或 创建后有新 commit（§3.11 自动清理）。"""
        wt = self._require(name)
        repo_path = Path(wt.path)
        if not repo_path.is_dir():
            return False  # 目录已消失视为无变更（避免反复 fail-closed）
        status = self._git("-C", str(repo_path), "status", "--porcelain").strip()
        if status:
            return True
        if wt.head_commit:
            new = self._git(
                "-C", str(repo_path), "log", "--oneline", f"{wt.head_commit}..{wt.branch}"
            )
            if new.strip():
                return True
        return False

    # ---- 自动清理（§3.12 子 Agent 场景） ----

    def auto_cleanup(self, name: str) -> bool:
        """无变更 → 删除返回 False；有变更 → 保留返回 True（供主 Agent review）。"""
        if not self.has_changes(name):
            self.remove(name)
            return False
        return True

    # ---- 删除（变更保护 fail-closed） ----

    def remove(self, name: str, *, force: bool = False) -> None:
        """§3.11 退出：有 uncommitted / 新 commit 且非 force → 拒绝（防误删工作成果）。"""
        wt = self._require(name)
        repo_path = Path(wt.path)
        if not force and self.has_changes(name):
            raise WorktreeError(
                f"worktree {name} 有未提交变更/新 commit，拒绝删除；"
                "确认后 force=True（review 后放弃再删）"
            )
        if repo_path.is_dir():
            self._git("worktree", "remove", "--force", str(repo_path))
            time.sleep(0.1)  # git worktree remove 后 lockfile 释放等待
        self._git("branch", "-D", wt.branch)
        del self._active[name]
        self._save()

    # ---- 过期清理漏斗（孤儿安全网） ----

    def cleanup_expired(self, *, force: bool = False) -> int:
        """临时命名（temp_prefix 前缀）→ 过期 → fail-closed 变更检查（§3.11）。

        用户 `/worktree create my-feature` 不匹配 `agent-` 前缀，永不清。
        有变更或未推送 commit 的宁可多占磁盘也不丢成果。
        """
        now = time.time()
        removed = 0
        for wt in self.list():
            if not wt.name.startswith(self.temp_prefix):
                continue  # 非临时命名，跳过
            if now - wt.created <= self.max_age:
                continue  # 未过期
            if force or not self.has_changes(wt.name):
                try:
                    self.remove(wt.name, force=True)
                    removed += 1
                except WorktreeError:
                    pass  # 单个失败不阻断全量清理
        return removed

    def _require(self, name: str) -> Worktree:
        wt = self._active.get(name)
        if wt is None:
            raise WorktreeError(f"worktree 不存在：{name}")
        return wt

    def _git(self, *args: str) -> str:
        """git 子进程：三重防挂起（无提示终端 + 空 askpass + stdin 忽略）。"""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                env=_GIT_ENV,
                input="",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise WorktreeError("未找到 git 命令（Git for Windows 需安装并在 PATH）") from exc
        except subprocess.TimeoutExpired as exc:
            raise WorktreeError(f"git 调用超时：{args!r}") from exc
        if proc.returncode != 0:
            raise WorktreeError(f"git {args[0]} 失败：{proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def _save(self) -> None:
        self.worktree_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "repo_root": str(self.repo_root),
            "worktrees": {name: wt.to_dict() for name, wt in self._active.items()},
        }
        self._session_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load(self) -> None:
        """跨会话恢复：磁盘记录里目录已消失的孤儿丢弃（git worktree prune 后自然清理）。"""
        if not self._session_file.exists():
            return
        try:
            data = json.loads(self._session_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for name, raw in (data.get("worktrees") or {}).items():
            try:
                wt = Worktree.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if Path(wt.path).is_dir():
                self._active[name] = wt
