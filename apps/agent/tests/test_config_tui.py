"""The full-screen `moko config` navigator — editors (incl. the in-frame Brain
flow), the field model, navigation, and a render smoke test. The interactive
loop needs a real tty, so we exercise the pieces under it."""

from moko.cli import config_tui as ct
from moko.config.config import Config
from moko.tui import keys as K


def _cfg() -> Config:
    return Config(
        companion_name="Andrew",
        timezone="Australia/Brisbane",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
        interests=["calendar"],
        llm_provider="anthropic",
        llm_model="claude-opus-4-8",
    )


# ── editors ──────────────────────────────────────────────────────────────────


def test_list_editor_starts_on_current_and_commits():
    ed = ct.ListEditor("pick", [("a", 1), ("b", 2), ("c", 3)], current=2)
    assert ed.idx == 1  # pre-highlights the current value
    assert ed.handle(K.Key(K.DOWN)) is None
    assert ed.handle(K.Key(K.ENTER)) == ("commit", 3)
    assert ct.ListEditor("p", [("a", 1)], 1).handle(K.Key(ct.ESCAPE)) == ("cancel", None)


def test_list_editor_search_filters_then_commits():
    zones = [("Australia/Brisbane", "Australia/Brisbane"),
             ("Australia/Sydney", "Australia/Sydney"),
             ("Europe/Paris", "Europe/Paris")]
    ed = ct.ListEditor("tz", zones, "Europe/Paris", searchable=True)
    for ch in "brisb":
        ed.handle(K.Key(K.TEXT, ch))
    assert [c[1] for c in ed._visible()] == ["Australia/Brisbane"]
    assert ed.handle(K.Key(K.ENTER)) == ("commit", "Australia/Brisbane")


def test_list_editor_backspace_edits_query_then_cancels():
    ed = ct.ListEditor("tz", [("a", "a")], searchable=True)
    ed.handle(K.Key(K.TEXT, "x"))
    assert ed.handle(K.Key(K.BACKSPACE)) is None  # clears the query, not a cancel
    assert ed.filter == ""
    assert ed.handle(K.Key(K.BACKSPACE)) == ("cancel", None)  # empty query → cancel


def test_check_editor_toggles_and_keeps_declared_order():
    ed = ct.CheckEditor("help", [("x", "x"), ("y", "y"), ("z", "z")], checked=["z"])
    ed.handle(K.Key(K.TEXT, " "))  # toggle x on (cursor at 0)
    assert ed.handle(K.Key(K.ENTER)) == ("commit", ["x", "z"])  # declared order


def test_text_editor_types_deletes_masks_and_commits():
    ed = ct.TextEditor("name", "Al")
    ed.handle(K.Key(K.TEXT, "ex"))
    ed.handle(K.Key(K.BACKSPACE))
    assert ed.handle(K.Key(K.ENTER)) == ("commit", "Ale")
    masked = ct.TextEditor("key", "abc", mask=True)
    assert "●●●" in str(masked.render(40)[2])  # value is masked in the render


def test_text_editor_validation_blocks_then_clears():
    ed = ct.TextEditor("model", "", validate=ct._required("a model name"))
    assert ed.handle(K.Key(K.ENTER)) is None  # empty → blocked, not committed
    assert "a model name" in ed.error
    assert any("a model name" in str(line) for line in ed.render(40))  # shown inline
    ed.handle(K.Key(K.TEXT, "opus"))  # typing clears the error
    assert ed.error == ""
    assert ed.handle(K.Key(K.ENTER)) == ("commit", "opus")


def test_http_validator():
    assert ct._http_url("not-a-url") is not None
    assert ct._http_url("https://api.x.ai/v1") is None


def test_list_editor_vim_keys_move_a_plain_list():
    ed = ct.ListEditor("pick", [("a", 1), ("b", 2), ("c", 3)], current=1)
    ed.handle(K.Key(K.TEXT, "j"))  # down
    ed.handle(K.Key(K.TEXT, "j"))  # down
    assert ed.handle(K.Key(K.ENTER)) == ("commit", 3)
    ed2 = ct.ListEditor("pick", [("a", 1), ("b", 2)], current=2, searchable=True)
    ed2.handle(K.Key(K.TEXT, "j"))  # in a searchable list, 'j' is a query char
    assert ed2.filter == "j"


# ── the in-frame Brain composite ──────────────────────────────────────────────


def test_brain_editor_none_finishes_immediately(monkeypatch):
    config = _cfg()
    saved: list[bool] = []
    monkeypatch.setattr(config, "save", lambda: saved.append(True))
    ed = ct.BrainEditor(config)
    # LLM_PROVIDERS ends with "none"; UP from the top wraps straight to it.
    ed.handle(K.Key(K.UP))
    assert ed.handle(K.Key(K.ENTER)) == ("commit", None)
    assert config.llm_provider == "none"
    assert saved == [True]


def test_brain_editor_anthropic_api_key_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))  # isolate secrets: no key on file yet
    config = _cfg()
    monkeypatch.setattr(config, "save", lambda: None)
    keyed: list[tuple] = []
    from moko.config import secrets
    monkeypatch.setattr(secrets, "set_api_key", lambda p, k: keyed.append((p, k)))

    ed = ct.BrainEditor(config)
    ed.handle(K.Key(K.ENTER))          # provider: anthropic (current, idx 0)
    ed.handle(K.Key(K.ENTER))          # model: opus (current, idx 0)
    ed.handle(K.Key(K.UP))             # auth: wrap up to the last option = api_key
    ed.handle(K.Key(K.ENTER))          # choose api_key → key step
    for ch in "sk-test":
        ed.handle(K.Key(K.TEXT, ch))
    assert ed.handle(K.Key(K.ENTER)) == ("commit", None)
    assert config.llm_provider == "anthropic"
    assert config.llm_auth_method == "api_key"
    assert keyed == [("anthropic", "sk-test")]


def test_brain_editor_keeps_saved_key_without_repaste(tmp_path, monkeypatch):
    # A key is already on file for the new provider → the auth menu offers "keep" and
    # choosing it commits with no re-paste and no overwrite of the stored secret.
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.config import secrets
    secrets.set_api_key("openai", "already-here")
    config = _cfg()
    monkeypatch.setattr(config, "save", lambda: None)
    overwrites: list[tuple] = []
    monkeypatch.setattr(secrets, "set_api_key",
                        lambda p, k: overwrites.append((p, k)))  # must NOT be called

    ed = ct.BrainEditor(config)
    assert ed._advance("openai") is None and ed.step == "model"
    assert ed._advance("gpt-5") is None and ed.step == "auth"
    # First auth choice is "keep" (no claude_code/oauth for openai), so plain ENTER picks it.
    assert ("keep") in [v for _, v in ed.sub.choices]
    assert ed._advance("keep") == ("commit", None)
    assert config.llm_provider == "openai"
    assert config.llm_auth_method == "api_key"
    assert overwrites == []  # the saved key was reused, not rewritten
    assert secrets.get_api_key("openai") == "already-here"


def test_brain_editor_blank_key_keeps_saved_one(tmp_path, monkeypatch):
    # Choosing "Paste a different API key" then submitting blank also keeps the saved key.
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.config import secrets
    secrets.set_api_key("openai", "already-here")
    config = _cfg()
    monkeypatch.setattr(config, "save", lambda: None)
    overwrites: list[tuple] = []
    monkeypatch.setattr(secrets, "set_api_key", lambda p, k: overwrites.append((p, k)))

    ed = ct.BrainEditor(config)
    ed._advance("openai")
    ed._advance("gpt-5")
    assert ed._advance("api_key") is None and ed.step == "key"
    assert ed.sub.validate is None  # blank is allowed when a key is already stored
    assert ed._advance("") == ("commit", None)  # blank → keep
    assert config.llm_auth_method == "api_key"
    assert overwrites == []


# ── field model + apply ──────────────────────────────────────────────────────


def test_model_apply_writes_through_to_config():
    config = _cfg()
    fields = {f.key: f for cat in ct._build_model() for f in cat.fields}
    fields["name"].apply(config, "Drew")
    fields["presence"].apply(config, "chatty")
    fields["settle"].apply(config, 45)
    fields["interests"].apply(config, ["calendar", "curious"])
    assert config.companion_name == "Drew"
    assert config.proactivity == "chatty"
    assert config.cadence.outreach_settle_minutes == 45
    assert config.interests == ["calendar", "curious"]


def test_minutes_choices_include_current_value():
    values = [v for _, v in ct._minutes((15, 30, 60), current=37)]
    assert 37 in values and values == sorted(values)


def test_summaries_render_current_values():
    config = _cfg()
    fields = {f.key: f for cat in ct._build_model() for f in cat.fields}
    assert fields["name"].summary(config) == "Andrew"
    assert fields["interests"].summary(config) == "1 selected"
    assert "min" in fields["muse"].summary(config)


def test_brain_and_access_fields_are_self_persisting():
    # Composite editors save themselves, so the navigator must not double-apply.
    fields = {f.key: f for cat in ct._build_model() for f in cat.fields}
    assert fields["model"].apply is None
    assert fields["tier"].apply is None


# ── navigation ────────────────────────────────────────────────────────────────


class _FakeTerminal:
    """Feeds a scripted list of keys; falls back to Ctrl+C so a wrong script can
    never hang the loop. Stands in for the real cbreak tty in tests."""

    def __init__(self, keys):
        self._keys = list(keys)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_key(self):
        return self._keys.pop(0) if self._keys else K.Key(K.INTERRUPT)


class _FakeLive:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, *a, **k):
        pass

    def refresh(self):
        pass


def test_run_navigates_with_arrows_edits_and_saves(monkeypatch):
    config = _cfg()
    saved: list[bool] = []
    monkeypatch.setattr(config, "save", lambda: saved.append(True))
    # Offline in this test, so quitting doesn't try to restart a real service.
    monkeypatch.setattr(ct, "_service_online", lambda: False)

    # Identity → DOWN to Rhythm → RIGHT into its fields → DOWN×2 to Presence →
    # RIGHT to open the select → DOWN to "chatty" → ENTER commit → LEFT out → ESC.
    keys = [
        K.Key(K.DOWN),          # categories: Identity → Rhythm
        K.Key(K.RIGHT),         # enter Rhythm's fields (right arrow drills in)
        K.Key(K.DOWN), K.Key(K.DOWN),  # → Presence
        K.Key(K.RIGHT),         # open the presence select (current "moderate")
        K.Key(K.DOWN),          # moderate → chatty
        K.Key(K.ENTER),         # commit
        K.Key(K.LEFT),          # fields → categories (left arrow steps out)
        K.Key(ct.ESCAPE),       # quit
    ]
    monkeypatch.setattr(ct, "Terminal", lambda: _FakeTerminal(keys))
    monkeypatch.setattr(ct, "Live", _FakeLive)

    ct.run(config)
    assert config.proactivity == "chatty"
    assert saved == [True]  # saved exactly once, on commit


# ── render smoke test ─────────────────────────────────────────────────────────


def test_editing_while_online_marks_a_restart_pending_and_quit_applies(monkeypatch):
    config = _cfg()
    monkeypatch.setattr(config, "save", lambda: None)
    monkeypatch.setattr(ct, "Live", _FakeLive)
    monkeypatch.setattr(ct, "_service_online", lambda: True)
    restarts: list[bool] = []
    monkeypatch.setattr(ct, "_service_restart", lambda: restarts.append(True) or True)

    # Edit Presence (needs a restart), then quit → exactly one restart on the way out.
    keys = [K.Key(K.DOWN), K.Key(K.RIGHT), K.Key(K.DOWN), K.Key(K.DOWN), K.Key(K.RIGHT),
            K.Key(K.DOWN), K.Key(K.ENTER), K.Key(K.LEFT), K.Key(ct.ESCAPE)]
    monkeypatch.setattr(ct, "Terminal", lambda: _FakeTerminal(keys))
    ct.run(config)
    assert restarts == [True]  # applied once, on quit


def test_quit_without_changes_does_not_restart(monkeypatch):
    config = _cfg()
    monkeypatch.setattr(config, "save", lambda: None)
    monkeypatch.setattr(ct, "Live", _FakeLive)
    monkeypatch.setattr(ct, "_service_online", lambda: True)
    restarts: list[bool] = []
    monkeypatch.setattr(ct, "_service_restart", lambda: restarts.append(True) or True)
    monkeypatch.setattr(ct, "Terminal", lambda: _FakeTerminal([K.Key(ct.ESCAPE)]))
    ct.run(config)
    assert restarts == []  # nothing changed → no restart on quit


def test_pending_banner_shows_only_when_online_and_pending():
    from io import StringIO

    from rich.console import Console

    from moko.theme import RICH_THEME
    config = _cfg()
    model = ct._build_model()

    def frame(**kw):
        b = StringIO()
        Console(file=b, width=100, theme=RICH_THEME, force_terminal=True,
                color_system="truecolor").print(
            ct._render(config, model, 0, 0, "categories", None, **kw))
        return b.getvalue()

    assert "restart pending" in frame(online=True, pending=True)
    assert "restart pending" not in frame(online=True, pending=False)
    assert "restart pending" not in frame(online=False, pending=True)  # nothing to apply
    # Mid-restart the title shows a transitional state, not a stale "online".
    restarting = frame(online=True, pending=True, restarting=True)
    assert "restarting" in restarting
    assert "online" not in restarting and "restart pending" not in restarting


def test_timezone_preview_reports_a_local_time():
    note = ct._timezone_preview("UTC")
    assert "there now" in note
    assert ct._timezone_preview("Not/AZone") == ""  # bad zone → no crash, no note


def test_restart_key_restarts_only_when_online(monkeypatch):
    config = _cfg()
    monkeypatch.setattr(config, "save", lambda: None)
    monkeypatch.setattr(ct, "Live", _FakeLive)

    for online, expected in ((True, [True]), (False, [])):
        calls: list[bool] = []
        monkeypatch.setattr(ct, "_service_online", lambda o=online: o)
        monkeypatch.setattr(ct, "_service_restart", lambda: calls.append(True) or True)
        monkeypatch.setattr(ct, "Terminal",
                            lambda: _FakeTerminal([K.Key(K.TEXT, "r"), K.Key(ct.ESCAPE)]))
        ct.run(config)
        assert calls == expected  # offline → no restart attempt


def test_access_editor_restart_uses_the_quiet_api(monkeypatch):
    from moko.cli import service
    monkeypatch.setattr(service, "service_state", lambda: {"active": True})
    monkeypatch.setattr(service, "restart_quiet", lambda: True)
    assert "Restarted" in ct.AccessEditor(_cfg())._restart()


# ── render smoke test ─────────────────────────────────────────────────────────


def test_render_produces_a_frame_in_each_mode():
    from rich.console import Console

    from moko.theme import RICH_THEME

    config = _cfg()
    model = ct._build_model()
    sink = Console(file=open("/dev/null", "w"), width=90, theme=RICH_THEME)  # noqa: SIM115
    sink.print(ct._render(config, model, 0, 0, "categories", None, online=True))
    sink.print(ct._render(config, model, 1, 2, "fields", None, saved_note="saved · 3:42 PM"))
    sink.print(ct._render(config, model, 0, 0, "categories", None, show_help=True))
    editor = model[0].fields[0].make_editor(config)
    sink.print(ct._render(config, model, 0, 0, "edit", editor))
