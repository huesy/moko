"""Moko's skills: capabilities described in editable markdown, not hard-coded
into a fat system prompt. See `loader.py`."""

from .loader import Skill, load_skills, render_skills

__all__ = ["Skill", "load_skills", "render_skills"]
