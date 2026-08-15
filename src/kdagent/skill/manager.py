"""SkillManager：三级搜索 / 两阶段加载 / skill-creator 落盘（规格 09 §3.8-3.9）。

三级优先级，同名高优先级覆盖低优先级（同 npm 包搜索路径）：
1. 项目级 {work_dir}/.kdagent/skills/   # 可提交 git、团队共享
2. 用户级 ~/.kdagent/skills/            # 个人通用
3. 内置级 程序包内 skills/builtin/       # 开箱即用

两个阶段：
- 第一阶段（启动 scan）：只解析每个 Skill 的 frontmatter（轻量），供
  system-reminder「可用 Skill」注入。
- 第二阶段（按需 load）：读完整正文 + $ARGUMENTS 替换，经 LoadSkill 注入对话。

Skill 文件可能被用户编辑，load 每次现读（不缓存正文），改动即时生效。
"""

from __future__ import annotations

from pathlib import Path

from kdagent.skill.model import (
    SKILL_LIST_LIMIT,
    SKILL_LIST_MAX_BYTES,
    SKILL_NAME_RE,
    Skill,
    SkillMeta,
    parse_skill_file,
    skill_body,
    yaml_scalar,
)


class SkillManager:
    """扫描目录注册 frontmatter；按需加载完整 SOP；skill-creator 写入目标。"""

    def __init__(self, dirs: list[Path], *, writable_dir: Path | None = None) -> None:
        self._dirs = list(dirs)  # 高优先级在前：[项目, 用户, 内置]
        self._writable_dir = writable_dir
        self._skills: dict[str, SkillMeta] = {}

    @property
    def writable_dir(self) -> Path:
        """skill-creator 落盘目标：显式指定优先，否则用户级（dirs[1]，无则首个）。"""
        if self._writable_dir is not None:
            return self._writable_dir
        return self._dirs[1] if len(self._dirs) > 1 else self._dirs[0]

    # ---- 第一阶段：轻量注册 ----

    def scan(self) -> None:
        """重扫三个目录（启动时 + skill-creator 创建后刷新）。高优先级覆盖低优先级。"""
        found: dict[str, SkillMeta] = {}
        for d in self._dirs:
            if not d.is_dir():
                continue
            for meta in _iter_skill_files(d):
                if meta.name not in found:
                    found[meta.name] = meta
        self._skills = found

    def list(self) -> list[SkillMeta]:
        """按名排序的轻量清单（system-reminder / /skills 查看用）。"""
        return sorted(self._skills.values(), key=lambda s: s.name)

    def get(self, name: str) -> SkillMeta | None:
        return self._skills.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    # ---- 第二阶段：按需加载 ----

    def load(self, name: str, arguments: str = "") -> Skill | None:
        """加载完整 SOP：读文件 + $ARGUMENTS 替换；不存在/读失败返回 None。

        每次现读（不缓存）——用户编辑 Skill 后下次 LoadSkill 即见新内容。
        """
        meta = self._skills.get(name)
        if meta is None or meta.path is None:
            return None
        try:
            text = meta.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return Skill(
            name=meta.name,
            description=meta.description,
            mode=meta.mode,
            model=meta.model,
            context=meta.context,
            path=meta.path,
            body=skill_body(text).replace("$ARGUMENTS", arguments),
        )

    # ---- skill-creator（T24）：落盘 + 刷新索引 ----

    def create(self, name: str, description: str, body: str, *, mode: str = "inline") -> Path:
        """写一个新 Skill 到 writable_dir 并刷新索引。

        name 非法 → ValueError；同名已存在 → FileExistsError（拒绝静默覆盖用户内容）。
        """
        name = name.strip().lower()
        if not SKILL_NAME_RE.fullmatch(name):
            raise ValueError(
                "Skill name 需为小写字母/数字/连字符，且以字母或数字开头"
            )
        target = self.writable_dir / f"{name}.md"
        if target.exists():
            raise FileExistsError(f"Skill 已存在：{name}（可先 ReadFile 查看再改）")
        front = (
            f"---\n"
            f"name: {name}\n"
            f"description: {yaml_scalar(description.strip())}\n"
            f"mode: {mode if mode in ('inline', 'fork') else 'inline'}\n"
            f"---\n\n"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(front + body.strip() + "\n", encoding="utf-8")
        self.scan()
        return target


def build_skills_reminder(skills: list[SkillMeta]) -> str | None:
    """09 §3.9/§3.12：可用 Skill system-reminder（上限 100 条 / 8KB）；无 Skill 返回 None。

    只列 name + description（渐进式披露），完整 SOP 经 LoadSkill 按需加载。
    """
    if not skills:
        return None
    lines = [f"- {s.name}：{s.description}" for s in skills[:SKILL_LIST_LIMIT]]
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > SKILL_LIST_MAX_BYTES:
        # 按字符逐步截断到 8KB 内（保证不切碎 UTF-8 序列）
        for cut in range(len(text), 0, -1):
            if len(text[:cut].encode("utf-8")) <= SKILL_LIST_MAX_BYTES:
                text = text[:cut] + "…"
                break
    return (
        "<system-reminder>\n可用 Skill（完整 SOP 经 LoadSkill 加载）：\n"
        + text
        + "\n</system-reminder>"
    )


def _iter_skill_files(d: Path) -> list[SkillMeta]:
    """扫描目录内 Skill：单文件顶层 *.md + 目录型 */SKILL.md（入口）。

    顶层 SKILL.md（无名字上下文）跳过。坏文件（无 frontmatter）跳过。
    """
    metas: list[SkillMeta] = []
    for p in sorted(d.glob("*.md")):
        if p.name == "SKILL.md":
            continue
        meta = parse_skill_file(p)
        if meta is not None:
            metas.append(meta)
    for p in sorted(d.glob("*/SKILL.md")):
        meta = parse_skill_file(p)
        if meta is not None:
            metas.append(meta)
    return metas
