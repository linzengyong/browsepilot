"""Skill 加载器：渐进式披露、关键词匹配、热加载"""
import re
import yaml
from pathlib import Path
from dataclasses import dataclass
from typing import ClassVar
from loguru import logger


@dataclass
class Skill:
    name: str
    description: str
    trigger_keywords: list[str]
    content: str
    source_dir: Path

    _registry: ClassVar[dict[str, "Skill"]] = {}
    _listing_cache: ClassVar[str | None] = None

    @classmethod
    def load_all(cls, skills_dir: Path | None = None) -> dict[str, "Skill"]:
        """加载 skills 目录下所有 SKILL.md 文件"""
        if skills_dir is None:
            skills_dir = Path(__file__).resolve().parent.parent.parent.parent / "skills"
        if not skills_dir.exists():
            return {}

        cls._registry = {}
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            try:
                skill = cls._parse(skill_file, skill_dir)
                cls._registry[skill.name] = skill
            except Exception as e:
                logger.warning(f"Failed to load skill {skill_dir.name}: {e}")

        cls._listing_cache = None
        return cls._registry

    @classmethod
    def _parse(cls, filepath: Path, source_dir: Path) -> "Skill":
        """解析 SKILL.md 的 frontmatter + 正文"""
        text = filepath.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not m:
            raise ValueError(f"Invalid SKILL.md format: {filepath}")

        meta = yaml.safe_load(m.group(1))
        return cls(
            name=meta["name"],
            description=meta.get("description", ""),
            trigger_keywords=meta.get("trigger_keywords", []),
            content=m.group(2).strip(),
            source_dir=source_dir,
        )

    @classmethod
    def get_listing(cls) -> str:
        """第一层：所有 skill 的名称+描述列表（每次 plan 注入，token 成本极低）"""
        if cls._listing_cache is not None:
            return cls._listing_cache

        if not cls._registry:
            cls.load_all()

        if not cls._registry:
            cls._listing_cache = ""
            return ""

        lines = ["## 可用领域知识 (Skills)", ""]
        for skill in cls._registry.values():
            lines.append(f"- **{skill.name}**: {skill.description}")
        lines.append("")
        lines.append("当你的任务匹配上述领域时，系统会自动加载对应的操作规范。")
        cls._listing_cache = "\n".join(lines)
        return cls._listing_cache

    @classmethod
    def match(cls, task: str) -> list["Skill"]:
        """第二层：根据用户任务匹配并返回完整的 skill 内容"""
        if not cls._registry:
            cls.load_all()

        matched = []
        for skill in cls._registry.values():
            for pattern in skill.trigger_keywords:
                try:
                    if re.search(pattern, task):
                        matched.append(skill)
                        break
                except re.error:
                    continue
        return matched

    @classmethod
    def format_matched(cls, matched: list["Skill"]) -> str:
        """将匹配到的 skill 格式化为 prompt 注入内容"""
        if not matched:
            return ""
        parts = ["---", "## 已激活的领域知识", ""]
        for skill in matched:
            parts.append(f"### Skill: {skill.name}")
            parts.append(skill.content)
            parts.append("")
        parts.append("**重要：严格遵守以上领域知识中的操作规范和注意事项。**")
        return "\n".join(parts)
