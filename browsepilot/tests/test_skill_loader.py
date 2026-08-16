"""Tests for skill loader module."""

import pytest

from backend.app.agent.skill_loader import Skill


def test_load_all_skills():
    """Verify all skills are loaded from the skills/ directory."""
    Skill.load_all()
    registry = Skill._registry
    assert len(registry) >= 1, f"Expected at least 1 skill, got {len(registry)}"
    assert "ecommerce-compare" in registry


def test_skill_frontmatter():
    """Verify frontmatter fields are correctly parsed."""
    Skill.load_all()
    skill = Skill._registry["ecommerce-compare"]
    assert skill.name == "ecommerce-compare"
    assert len(skill.trigger_keywords) > 0
    assert len(skill.content) > 50


def test_listing_cache():
    """Verify listing is generated and cached."""
    Skill.load_all()
    listing = Skill.get_listing()
    assert "可用领域知识" in listing
    assert "ecommerce-compare" in listing
    listing2 = Skill.get_listing()
    assert listing2 == listing


@pytest.mark.parametrize(
    "task,expected_skill",
    [
        ("去京东搜机械键盘对比价格", "ecommerce-compare"),
        ("帮我比价淘宝和拼多多", "ecommerce-compare"),
        ("推荐三款500元的键盘", "ecommerce-compare"),
    ],
)
def test_skill_matching(task, expected_skill):
    """Verify skills are correctly matched to tasks."""
    matched = Skill.match(task)
    names = [s.name for s in matched]
    assert expected_skill in names, f"'{task}' should match {expected_skill}, got {names}"


def test_format_matched():
    """Verify matched skills are formatted correctly."""
    matched = Skill.match("去京东搜机械键盘")
    formatted = Skill.format_matched(matched)
    assert "已激活的领域知识" in formatted
    assert "### Skill:" in formatted
    assert "严格遵守" in formatted


def test_empty_match():
    """Verify no-match returns empty string."""
    formatted = Skill.format_matched([])
    assert formatted == ""


def test_invalid_file(tmp_path):
    """Verify corrupted SKILL.md raises ValueError."""
    invalid_file = tmp_path / "SKILL.md"
    invalid_file.write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(ValueError):
        Skill._parse(invalid_file, tmp_path)
