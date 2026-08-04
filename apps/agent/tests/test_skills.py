"""Skills loader: discovery, frontmatter parsing, user override, rendering."""

from moko.skills import load_skills, render_skills
from moko.skills.loader import Skill, _parse, _split_frontmatter

CORE = {"acting", "curiosity", "honesty", "inner-life", "integrate-service", "recalling",
        "remembering", "web"}


def _write_skill(root, name, *, description="", body="guidance", frontmatter_name=None):
    d = root / name
    d.mkdir(parents=True)
    fm_name = frontmatter_name if frontmatter_name is not None else name
    (d / "SKILL.md").write_text(
        f"---\nname: {fm_name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
    )


# ── built-in skills ────────────────────────────────────────────────────
def test_builtin_skills_load_sorted_with_bodies():
    skills = load_skills()
    assert {s.name for s in skills} == CORE
    assert [s.name for s in skills] == sorted(s.name for s in skills)  # byte-stable order
    assert all(s.body for s in skills)


def test_builtin_remembering_carries_the_rules():
    body = next(s for s in load_skills() if s.name == "remembering").body
    assert "graphic designer" in body  # the refinement rule moved here intact
    assert "speaker_sentiment" in body


# ── user skills ────────────────────────────────────────────────────────
def test_user_skill_adds_alongside_builtins(tmp_path):
    _write_skill(tmp_path, "morning_brief", body="Summarise the day ahead.")
    skills = load_skills(tmp_path)
    names = {s.name for s in skills}
    assert names == CORE | {"morning_brief"}
    assert next(s for s in skills if s.name == "morning_brief").body == "Summarise the day ahead."


def test_user_skill_overrides_builtin_by_name(tmp_path):
    _write_skill(tmp_path, "honesty", body="MY custom honesty rule.")
    honesty = next(s for s in load_skills(tmp_path) if s.name == "honesty")
    assert honesty.body == "MY custom honesty rule."


def test_missing_user_dir_yields_builtins_only(tmp_path):
    assert {s.name for s in load_skills(tmp_path / "nope")} == CORE


# ── parsing edge cases ─────────────────────────────────────────────────
def test_split_frontmatter_extracts_meta_and_body():
    meta, body = _split_frontmatter("---\nname: x\ndescription: d\n---\nthe body")
    assert meta == {"name": "x", "description": "d"}
    assert body.strip() == "the body"


def test_parse_falls_back_to_dir_name_without_frontmatter(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "SKILL.md").write_text("just a body, no frontmatter", encoding="utf-8")
    skill = _parse(d / "SKILL.md")
    assert skill == Skill(name="plain", description="", body="just a body, no frontmatter")


def test_parse_rejects_empty_body(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: empty\n---\n   \n", encoding="utf-8")
    assert _parse(d / "SKILL.md") is None


def test_malformed_frontmatter_is_tolerated(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("---\n: : not yaml : :\n---\nbody survives", encoding="utf-8")
    skill = _parse(d / "SKILL.md")
    assert skill is not None and "body survives" in skill.body


# ── rendering ──────────────────────────────────────────────────────────
def test_render_skills_headers_and_empty():
    assert render_skills([]) == ""
    out = render_skills([Skill("a", "", "alpha"), Skill("b", "", "beta")])
    assert "# Your skills" in out
    assert "## a\n\nalpha" in out and "## b\n\nbeta" in out
