"""The setup-wizard string catalogue: English source of truth, JA/ES translations,
and graceful fallback for languages we offer but don't fully translate."""

import re

import moko.i18n as i18n
from moko.i18n.en import STRINGS as EN


def test_english_is_the_source():
    assert i18n.t("name.title") == EN["name.title"]


def test_translated_locales_present():
    assert {"en", "ja", "es"} <= i18n.TRANSLATED


def test_untranslated_language_falls_back_to_english():
    # French is offered (Moko will converse in it) but its UI falls back to English.
    assert "fr" in {code for code, _, _ in i18n.LANGUAGES}
    assert i18n.t("name.title", "fr") == EN["name.title"]


def test_missing_key_returns_the_key_loudly():
    assert i18n.t("does.not.exist", "ja") == "does.not.exist"


def test_placeholders_are_filled():
    out = i18n.t("summary.farewell", "es", name="Ana")
    assert "Ana" in out
    assert "{name}" not in out


def test_translated_locales_match_keys_and_placeholders():
    for code in ("ja", "es"):
        catalog = i18n._CATALOGS[code]
        assert set(catalog) == set(EN), f"{code}: key set differs from English"
        for key, en_value in EN.items():
            en_ph = set(re.findall(r"\{(\w+)\}", en_value))
            tr_ph = set(re.findall(r"\{(\w+)\}", catalog[key]))
            assert en_ph == tr_ph, f"{code}/{key}: placeholder drift {en_ph} vs {tr_ph}"


def test_terms_body_keeps_its_paragraphs_in_every_locale():
    # The consent screen's promises are separate paragraphs; keep that structure so
    # the ScrollEditor renders them with breathing room everywhere.
    for code in ("en", "ja", "es"):
        body = i18n.t("terms.body", code)
        assert body.count("\n\n") >= 4


def test_language_choices_mark_fully_translated():
    by_code = {code: label for label, code in i18n.language_choices()}
    assert "✓" in by_code["en"]
    assert "✓" in by_code["ja"]
    assert "✓" not in by_code["fr"]


def test_complex_scripts_stay_out_of_the_aligned_grid():
    # RTL (Arabic) and combining-mark (Devanagari) scripts wrap/misalign a fixed-width
    # terminal grid — and rich pads every line by an undercounted width, so they can't
    # appear ANYWHERE in the frame. The picker shows the English name; cleanly-aligning
    # scripts (CJK, Cyrillic, Latin) stay native.
    by_code = {code: label for label, code in i18n.language_choices()}
    assert "العربية" not in by_code["ar"] and "Arabic" in by_code["ar"]
    assert "हिन्दी" not in by_code["hi"] and "Hindi" in by_code["hi"]
    assert "日本語" in by_code["ja"]  # CJK aligns fine → shown natively
    assert "Русский" in by_code["ru"]


def test_language_name_is_grid_safe():
    # The display name (summary / config value column) never returns an RTL or complex
    # script that would wrap a padded cell — English name for those, native otherwise.
    assert i18n.language_name("ja") == "日本語"
    assert i18n.language_name("ru") == "Русский"
    assert i18n.language_name("en") == "English"
    assert i18n.language_name("hi") == "Hindi"  # not हिन्दी
    assert i18n.language_name("ar") == "Arabic"  # not العربية
