"""A tiny string catalogue for the setup wizard — the one surface a companion
meets before Moko can speak for itself, so it's worth meeting them in their
language.

English is the source of truth (:mod:`moko.i18n.en`); other locales translate
the same keys and fall back to English for anything they're missing. The picker
offers more languages than we fully translate on purpose: even when the *setup
chrome* is English, Moko still records the choice and **converses** in that
language, since its replies are model-generated.

Deliberately not gettext/Babel: no build step, no .po compilation, no new
dependency — just dicts, which is all M0-scale onboarding needs.
"""

from __future__ import annotations

import contextlib
import unicodedata
from importlib import import_module

from .en import STRINGS as _EN

# Locales with a full UI catalogue. Imported lazily so a half-finished locale
# (or none at all) never breaks startup — English always works.
_CATALOGS: dict[str, dict[str, str]] = {"en": _EN}
for _code in ("ja", "es"):
    with contextlib.suppress(ImportError, AttributeError):  # locale not shipped yet
        _CATALOGS[_code] = import_module(f".{_code}", __name__).STRINGS

#: Locale codes that have a real UI translation (English always present).
TRANSLATED: frozenset[str] = frozenset(_CATALOGS)

# Languages offered in the picker: (code, endonym, English name). The fully-
# translated ones lead; the rest still set Moko's conversational language.
LANGUAGES: list[tuple[str, str, str]] = [
    ("en", "English", "English"),
    ("ja", "日本語", "Japanese"),
    ("es", "Español", "Spanish"),
    ("fr", "Français", "French"),
    ("de", "Deutsch", "German"),
    ("pt", "Português", "Portuguese"),
    ("it", "Italiano", "Italian"),
    ("zh", "中文", "Chinese"),
    ("ko", "한국어", "Korean"),
    ("hi", "हिन्दी", "Hindi"),
    ("ar", "العربية", "Arabic"),
    ("ru", "Русский", "Russian"),
]

DEFAULT_LANG = "en"


def t(key: str, lang: str = DEFAULT_LANG, /, **kw: object) -> str:
    """The string for ``key`` in ``lang``: the locale's value, else English, else
    the key itself (so a missing string shows up loudly rather than blank).
    ``**kw`` fills ``{placeholder}`` fields."""
    catalog = _CATALOGS.get(lang, _EN)
    value = catalog.get(key)
    if value is None:
        value = _EN.get(key, key)
    return value.format(**kw) if kw else value


def _grid_safe(text: str) -> bool:
    """Whether ``text`` renders predictably in a fixed-width terminal grid. RTL runs
    (Arabic, Hebrew) make the terminal reorder the whole line, and combining marks
    (Devanagari and other complex scripts) are width-counted differently by the
    terminal than by any width table — so a padded row overflows and wraps, which
    desyncs the whole frame. Such scripts are kept out of the aligned list."""
    return not any(
        unicodedata.bidirectional(ch) in ("R", "AL", "AN") or unicodedata.combining(ch)
        for ch in text
    )


def language_choices() -> list[tuple[str, str]]:
    """``[(label, code), …]`` for the language picker — the native endonym (with the
    English name, so search matches either) where a terminal grid can align it, else
    just the English name; a ✓ marks the locales fully translated here. The endonym a
    row can't show still surfaces via :func:`language_endonym` on the active row."""
    out = []
    for code, endonym, english in LANGUAGES:
        if endonym == english or not _grid_safe(endonym):
            label = english
        else:
            label = f"{endonym} · {english}"
        if code in TRANSLATED:
            label += "  ✓"
        out.append((label, code))
    return out


def language_name(lang: str) -> str:
    """A display name safe for a fixed-width frame: the native endonym where a
    terminal grid can align it (Latin/CJK/Cyrillic), else the English name — so an
    RTL/complex script never lands in a padded cell and wraps. Used by the summary
    and the config editor's value column. Falls back to the code if unknown."""
    for code, endonym, english in LANGUAGES:
        if code == lang:
            return endonym if _grid_safe(endonym) else english
    return lang
