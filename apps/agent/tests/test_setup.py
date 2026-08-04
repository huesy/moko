"""The first-run wizard. The interactive loop needs a real tty, so we drive the
state machine directly through `Wizard.feed()` with synthesised key events, and
check `setup.run()`'s terminal guard."""

import moko.i18n as i18n
from moko.cli import setup, wizard
from moko.cli._tui import ESCAPE
from moko.config.config import Config
from moko.tui import keys as K


def _enter():
    return K.Key(K.ENTER)


def _text(s):
    return K.Key(K.TEXT, s)


def _esc():
    return K.Key(ESCAPE)


def _left():
    return K.Key(K.LEFT)


def _key(w):
    return w.steps[w.idx].key


# ── the flow ─────────────────────────────────────────────────────────────────


def test_happy_path_completes_and_builds_config():
    w = wizard.Wizard(None)
    assert _key(w) == "welcome"
    w.feed(_enter())  # welcome → language
    assert _key(w) == "language"
    w.feed(_enter())  # English → terms
    assert _key(w) == "terms"
    assert w.accepted is False
    w.feed(_enter())  # accept terms
    assert w.accepted is True
    assert _key(w) == "name"

    for ch in "Andrew":
        w.feed(_text(ch))
    w.feed(_enter())
    assert w.config.companion_name == "Andrew"
    assert _key(w) == "timezone"

    for ch in "Tokyo":  # filter the searchable zone list
        w.feed(_text(ch))
    w.feed(_enter())
    assert w.config.timezone == "Asia/Tokyo"
    assert _key(w) == "presence"

    for _ in ("presence", "research", "sleep", "wake"):
        w.feed(_enter())
    # The research step defaults to the safe "No" (autonomous research off), so tapping
    # through it leaves the opt-in unset.
    assert w.config.allow_autonomous_research is False
    assert _key(w) == "brain"
    w.feed(_enter())  # "I'll decide later" is preselected → finishes the brain step
    assert w.config.llm_provider == "none"
    assert _key(w) == "senses"
    w.feed(_enter())  # no extra senses ticked
    assert _key(w) == "channels"
    assert w.feed(_enter()) == "completed"  # Telegram is pre-ticked
    assert w.channels == {"telegram"}


def test_name_is_capped_at_64_chars():
    w = wizard.Wizard(None)
    for _ in range(3):  # to the name step
        w.feed(_enter())
    assert _key(w) == "name"
    for ch in "x" * 100:
        w.feed(_text(ch))
    assert len(w._editor.buf) == 64


def test_timezone_step_is_searchable_and_sets_the_zone():
    w = wizard.Wizard(None)
    tz = next(s for s in w.steps if s.key == "timezone")
    ed = tz.make_editor(w)
    assert ed.searchable and len(ed.choices) > 100  # the full IANA list, filterable
    tz.apply(w, "Europe/Paris")
    assert w.config.timezone == "Europe/Paris"


def test_interests_step_is_gone():
    assert "interests" not in [s.key for s in wizard.Wizard(None).steps]


def test_research_step_defaults_off_and_can_opt_in():
    w = wizard.Wizard(None)
    step = next(s for s in w.steps if s.key == "research")
    ed = step.make_editor(w)
    # Boolean choices; the safe default (off) is what a fresh config carries.
    assert [v for _, v in ed.choices] == [True, False]
    assert w.config.allow_autonomous_research is False
    step.apply(w, True)
    assert w.config.allow_autonomous_research is True


def test_terms_decline_exits():
    w = wizard.Wizard(None)
    w.feed(_enter())  # → language
    w.feed(_enter())  # → terms
    assert _key(w) == "terms"
    assert w.feed(_esc()) == "declined"


def test_welcome_escape_cancels():
    assert wizard.Wizard(None).feed(_esc()) == "cancelled"


def test_left_steps_back():
    w = wizard.Wizard(None)
    for _ in range(3):  # welcome → language → terms → name
        w.feed(_enter())
    assert _key(w) == "name"
    w.feed(_left())
    assert _key(w) == "terms"


def test_language_choice_switches_locale():
    w = wizard.Wizard(None)
    w.feed(_enter())  # → language
    w.feed(K.Key(K.DOWN))  # English → 日本語
    w.feed(_enter())
    assert w.lang == "ja"
    assert w.config.language == "ja"
    assert w.t("name.title") == i18n.t("name.title", "ja")


def test_selected_senses_unlock_their_steps():
    w = wizard.Wizard(None)
    img = next(s for s in w.steps if s.key == "sense:image")
    assert img.skip(w) is True  # nothing ticked → skipped
    w._apply_senses(["image"])
    assert img.skip(w) is False


def test_returning_companion_pre_fills_and_keeps_on_cancel():
    prior = Config(companion_name="Al", timezone="Australia/Brisbane", sleep_start="22:00",
                   sleep_end="06:00", proactivity="gentle", language="es")
    w = wizard.Wizard(prior)
    assert w.had_prior is True
    assert w.lang == "es"
    assert w.feed(_esc()) == "cancelled"  # esc on the welcome screen keeps things as they are


def test_render_smoke_every_step():
    w = wizard.Wizard(None)
    w.senses = set(wizard._SENSES)  # so each per-sense step builds an editor
    for i, step in enumerate(w.steps):
        w.idx = i
        if step.skip and step.skip(w):
            continue
        w._editor = None
        w._ensure_editor()
        assert w._frame(step) is not None  # must not raise


def test_setup_run_requires_a_tty(monkeypatch):
    class _NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(setup.sys, "stdin", _NoTTY())  # guard short-circuits on stdin
    assert setup.run() == 1
