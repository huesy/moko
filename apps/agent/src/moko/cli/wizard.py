"""`moko setup` — the first-meeting wizard: a warm, full-screen, stepped flow.

Where the live editor (:mod:`moko.cli.config_tui`) is a master-detail navigator
you dip into to change one thing, the wizard walks a companion through a single
ordered path the first time: a logo, their language, the terms they're agreeing
to, who they are and where (when) they are, Moko's brain and senses, and how
they'll talk. A breadcrumb across the top shows where they are.

It reuses the same in-frame primitives as the editor — the :mod:`moko.cli._tui`
widgets and the composite Brain/Modality editors from :mod:`moko.cli.config_tui`
— so a model/sign-in flow looks identical here and there. The flows that *can't*
run inside the alternate screen (Telegram pairing's getUpdates wait, the Grok
OAuth browser approval) happen on the bare console as the wizard tears down,
exactly as the editor already does.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import i18n
from ..clock import Clock, utc_isoformat
from ..config import config as cfg
from ..config import regions
from ..theme import MOKO_PINK, MOKO_PINK_DIM, console
from ..tui import keys as K
from . import catalog
from ._tui import (
    ESCAPE,
    MUTED,
    RESIZE,
    CheckEditor,
    ListEditor,
    ScrollEditor,
    Terminal,
    TextEditor,
)
from .config_tui import (
    BrainEditor,
    ModalityEditor,
    _finish_pending_logins,
    _timezone_choices,
    _timezone_preview,
)

# A pink wordmark for the opening screen (ANSI-shadow block letters).
_LOGO = [
    "███╗   ███╗  ██████╗  ██╗  ██╗  ██████╗ ",
    "████╗ ████║ ██╔═══██╗ ██║ ██╔╝ ██╔═══██╗",
    "██╔████╔██║ ██║   ██║ █████╔╝  ██║   ██║",
    "██║╚██╔╝██║ ██║   ██║ ██╔═██╗  ██║   ██║",
    "██║ ╚═╝ ██║ ╚██████╔╝ ██║  ██╗ ╚██████╔╝",
    "╚═╝     ╚═╝  ╚═════╝  ╚═╝  ╚═╝  ╚═════╝ ",
]

# The generative senses offered on the "extra senses" step, in order. Each maps to
# a per-modality ModalityEditor (shared with the live editor); vision is a chat
# capability (empty = use the brain), the rest are independent services.
_SENSES = ["image", "speech", "hearing", "vision", "video"]

# The breadcrumb groups, in order. Every step belongs to exactly one.
_GROUPS = ["welcome", "language", "terms", "you", "brain", "senses", "channels"]


def _set(config: cfg.Config, attr: str, value: Any) -> None:
    setattr(config, attr, value)


@dataclass
class Step:
    """One screen. ``make_editor(wizard)`` builds its in-frame editor; ``apply``
    writes the committed value back (composite editors persist themselves, so theirs
    is None); ``note`` is a one-line translated hint above the editor; ``skip`` hides
    the step when it doesn't apply; ``cancel`` is what ESC/decline means here."""

    key: str
    group: str
    make_editor: Callable[[Wizard], Any]
    apply: Callable[[Wizard, Any], None] | None = None
    note: Callable[[Wizard], str] | None = None
    skip: Callable[[Wizard], bool] | None = None
    cancel: str = "back"  # "back" | "exit" (decline) | "quit" (leave)


class _LogoEditor:
    """The opening screen: the wordmark, a tagline, and a prompt to begin."""

    def __init__(self, tagline: str, sub: str, prompt: str) -> None:
        self.tagline, self.sub, self.prompt = tagline, sub, prompt

    def handle(self, key: K.Key):
        if key.kind == K.ENTER:
            return ("commit", None)
        if key.kind in (ESCAPE, K.BACKSPACE):
            return ("cancel", None)
        return None

    def render(self, width: int) -> list[Text]:
        out = [Text("")]
        out += [Text("  " + line, style=MOKO_PINK) for line in _LOGO]
        out += [Text(""), Text("  " + self.tagline, style=MOKO_PINK_DIM)]
        if self.sub:
            out.append(Text("  " + self.sub, style=MUTED))
        out += [Text(""), Text("  " + self.prompt, style=MUTED)]
        return out

    hint = "⏎ begin · esc later"


class Wizard:
    """The stepped first-run flow. ``run()`` drives the full-screen frame and
    returns an outcome; ``finish()`` does the bare-console tail (pairing, sign-ins,
    the service offer, and the summary) on a completed run."""

    def __init__(self, prior: cfg.Config | None) -> None:
        self.had_prior = prior is not None
        self.config = prior or self._fresh()
        self.lang = self.config.language or "en"
        self.senses = {m for m in cfg.MODALITIES if self.config.model_for(m).configured}
        # Pre-tick Telegram for first-timers; leave it alone if already paired.
        self.channels: set[str] = set() if self.config.telegram_user_id else {"telegram"}
        self.accepted = False
        self.saved_path = None
        self.steps = self._build_steps()
        self.idx = 0
        self._editor: Any = None

    @staticmethod
    def _fresh() -> cfg.Config:
        return cfg.Config(
            companion_name="",
            timezone=regions.detect_timezone() or "UTC",
            sleep_start="23:00",
            sleep_end="07:00",
            proactivity="moderate",
            language="en",
        )

    def t(self, key: str, **kw: object) -> str:
        return i18n.t(key, self.lang, **kw)

    # ── step model ────────────────────────────────────────────────────

    def _build_steps(self) -> list[Step]:
        sense_steps = [
            Step(
                f"sense:{m}", "senses",
                lambda w, m=m: ModalityEditor(w.config, m),
                skip=lambda w, m=m: m not in w.senses,
            )
            for m in _SENSES
        ]
        return [
            Step("welcome", "welcome", lambda w: w._welcome_editor(), cancel="quit"),
            Step("language", "language",
                 lambda w: ListEditor(w.t("lang.title"), i18n.language_choices(), w.lang),
                 apply=lambda w, v: w._apply_language(v), note=lambda w: w.t("lang.note")),
            Step("terms", "terms",
                 lambda w: ScrollEditor(w.t("terms.title"), w.t("terms.body")),
                 apply=lambda w, v: w._apply_terms(v), cancel="exit"),
            Step("name", "you",
                 lambda w: TextEditor(w.t("name.title"), w.config.companion_name, maxlen=64),
                 apply=lambda w, v: _set(w.config, "companion_name", v) if v else None,
                 note=lambda w: w.t("name.note")),
            Step("timezone", "you",
                 lambda w: ListEditor(w.t("region.title"), _timezone_choices(), w.config.timezone,
                                      searchable=True, preview=_timezone_preview),
                 apply=lambda w, v: _set(w.config, "timezone", v),
                 note=lambda w: w.t("region.note")),
            Step("presence", "you",
                 lambda w: ListEditor(w.t("presence.title"), w._presence_choices(),
                                      w.config.proactivity),
                 apply=lambda w, v: _set(w.config, "proactivity", v)),
            Step("research", "you",
                 lambda w: ListEditor(w.t("research.title"), w._research_choices(),
                                      w.config.allow_autonomous_research),
                 apply=lambda w, v: _set(w.config, "allow_autonomous_research", v),
                 note=lambda w: w.t("research.note")),
            Step("sleep", "you",
                 lambda w: ListEditor(w.t("sleep.title"), _times(catalog.SLEEP_TIMES),
                                      w.config.sleep_start),
                 apply=lambda w, v: _set(w.config, "sleep_start", v),
                 note=lambda w: w.t("sleep.note")),
            Step("wake", "you",
                 lambda w: ListEditor(w.t("wake.title"), _times(catalog.WAKE_TIMES),
                                      w.config.sleep_end),
                 apply=lambda w, v: _set(w.config, "sleep_end", v)),
            Step("brain", "brain", lambda w: BrainEditor(w.config),
                 note=lambda w: w.t("brain.note")),
            Step("senses", "senses",
                 lambda w: CheckEditor(w.t("senses.title"), w._sense_choices(), sorted(w.senses)),
                 apply=lambda w, v: w._apply_senses(v), note=lambda w: w.t("senses.note")),
            *sense_steps,
            Step("channels", "channels",
                 lambda w: CheckEditor(w.t("channels.title"), w._channel_choices(),
                                       sorted(w.channels)),
                 apply=lambda w, v: w._apply_channels(v), note=lambda w: w.t("channels.note")),
        ]

    def _welcome_editor(self) -> _LogoEditor:
        prompt = self.t("welcome.again") if self.had_prior else self.t("welcome.begin")
        return _LogoEditor(self.t("welcome.tagline"), self.t("welcome.sub"), prompt)

    def _presence_choices(self) -> list[tuple[str, str]]:
        return [(self.t(f"presence.{v}"), v) for v, _ in catalog.PROACTIVITY]

    def _research_choices(self) -> list[tuple[str, bool]]:
        return [(self.t("research.yes"), True), (self.t("research.no"), False)]

    def _sense_choices(self) -> list[tuple[str, str]]:
        return [(self.t(f"senses.{m}"), m) for m in _SENSES]

    def _channel_choices(self) -> list[tuple[str, str, bool]]:
        tg = self.t("channels.telegram")
        if self.config.telegram_user_id:
            tg += f"  ({self.t('channels.connected')})"
        soon = f"  · {self.t('channels.soon')}"
        return [
            (tg, "telegram", True),
            (f"WhatsApp{soon}", "whatsapp", False),
            (f"Messenger{soon}", "messenger", False),
            (f"LINE{soon}", "line", False),
            (f"Discord{soon}", "discord", False),
        ]

    # ── apply handlers ────────────────────────────────────────────────

    def _apply_language(self, value: str) -> None:
        self.lang = value
        self.config.language = value

    def _apply_terms(self, _value: object) -> None:
        # Accepting is the commitment point: save now so the composite Brain/Modality
        # editors (which persist themselves) have a config file to write onto.
        self.accepted = True
        if not self.config.created_at:
            self.config.created_at = utc_isoformat()

    def _apply_senses(self, value: list[str]) -> None:
        self.senses = set(value)

    def _apply_channels(self, value: list[str]) -> None:
        self.channels = set(value)

    # ── the loop ──────────────────────────────────────────────────────

    def run(self) -> str:
        """Drive the frame until the companion finishes, declines, or leaves.
        Returns "completed" | "declined" | "cancelled"."""
        with Terminal() as term, Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                self._ensure_editor()
                live.update(self._frame(self.steps[self.idx]))
                live.refresh()
                outcome = self.feed(term.read_key())
                if outcome:
                    return outcome

    def _ensure_editor(self) -> None:
        if self._editor is None:
            self._editor = self.steps[self.idx].make_editor(self)

    def feed(self, key: K.Key) -> str | None:
        """Process one key against the current step. Returns an outcome string when
        the wizard is done ("completed"/"declined"/"cancelled"), else None. Split out
        from :meth:`run` so the flow is drivable headlessly (no terminal needed)."""
        self._ensure_editor()
        step = self.steps[self.idx]

        if key.kind in (K.INTERRUPT, K.EOF):
            return "cancelled"
        if key.kind == RESIZE:
            return None
        if key.kind == K.LEFT and step.cancel != "quit":  # ← always steps back
            if self._go_back():
                self._editor = None
            return None

        result = self._editor.handle(key)
        if result is None:
            return None
        action, value = result
        if action == "commit":
            if step.apply:
                step.apply(self, value)
            if self.accepted:
                self.saved_path = self.config.save()
            self._editor = None
            return None if self._advance() else "completed"
        # cancel (ESC / Backspace-at-empty)
        if step.cancel == "exit":
            return "declined"
        if step.cancel == "quit":
            return "cancelled"
        if self._go_back():
            self._editor = None
        return None

    def _advance(self) -> bool:
        j = self.idx + 1
        while j < len(self.steps):
            if not (self.steps[j].skip and self.steps[j].skip(self)):
                self.idx = j
                return True
            j += 1
        return False

    def _go_back(self) -> bool:
        j = self.idx - 1
        while j >= 0:
            if not (self.steps[j].skip and self.steps[j].skip(self)):
                self.idx = j
                return True
            j -= 1
        return False

    # ── rendering ─────────────────────────────────────────────────────

    def _frame(self, step: Step) -> Panel:
        size = shutil.get_terminal_size((80, 24))
        width, height = size.columns, size.lines
        inner = max(28, width - 6)

        lines = [self._breadcrumb(inner), Text("")]
        if step.note:
            note = step.note(self)
            if note:
                lines += [Text(f"  {note}", style=MUTED), Text("")]

        # Size scrolling editors (lists, the Terms body) to the rows actually left
        # after the breadcrumb/note and the panel chrome, so a long list scrolls in
        # place instead of overflowing the frame.
        if hasattr(self._editor, "set_viewport"):
            self._editor.set_viewport(max(3, height - len(lines) - 10))
        body = self._editor.render(inner)
        avail = max(6, height - len(lines) - 5)  # leave room for borders + subtitle
        for ln in body[:avail]:
            cell = ln.copy()
            cell.truncate(inner, overflow="ellipsis")
            lines.append(cell)

        # Each editor's hint already names its keys (move/choose/back); that's the
        # footer. (← also steps back everywhere — an undocumented nicety.)
        hint = getattr(self._editor, "hint", "") or self.t("nav.hint")
        sub = f"[muted]{hint}[/muted]"
        return Panel(Group(*lines), title="[accent]Moko · setup 🌸[/accent]", border_style="moko",
                     subtitle=sub, subtitle_align="left", padding=(1, 2), height=height)

    def _breadcrumb(self, width: int) -> Text:
        # Progress is shown by colour, not by marker glyphs: filled/hollow circles
        # (●◉○) are East-Asian *Ambiguous* width, so terminals that draw them
        # double-width make this line overflow and wrap — which silently desyncs the
        # whole frame. Group labels are only ever Latin or CJK (safe), so colour-only
        # is robust. The current step is also flagged with "›" (the same narrow marker
        # the list rows use).
        cur = _GROUPS.index(self.steps[self.idx].group)
        out = Text("  ")
        for i, group in enumerate(_GROUPS):
            label = self.t(f"group.{group}")
            if i == cur:
                out.append("› ", style=f"bold {MOKO_PINK}")
                out.append(label, style=f"bold {MOKO_PINK}")
            elif i < cur:
                out.append(label, style=MOKO_PINK_DIM)
            else:
                out.append(label, style=MUTED)
            out.append("   ", style=MUTED)
        out.truncate(width, overflow="ellipsis")
        return out

    # ── bare-console tail (completed run) ─────────────────────────────

    def finish(self) -> None:
        """After the frame tears down: connect chosen channels, finish any pending
        sign-in that needs a browser, offer the always-on service, and say hello."""
        if not self.config.created_at:
            self.config.created_at = utc_isoformat()
        self.saved_path = self.config.save()
        self._connect_channels()
        _finish_pending_logins(self.config)
        self._offer_service()
        self._summary()

    def _connect_channels(self) -> None:
        if "telegram" in self.channels and not self.config.telegram_user_id:
            from . import pair
            console.print()
            console.print(f"[muted]{self.t('channels.connect_after')}[/muted]")
            pair.setup_telegram(self.config)

    def _offer_service(self) -> None:
        from . import service
        if not self.config.telegram_user_id:
            return  # nothing to stay online for yet
        if not service.service_state().get("available"):
            console.print(
                "[muted]Bring me online any time with [accent]moko start[/accent].[/muted]")
            return
        import questionary

        from ..theme import QUESTIONARY_STYLE
        console.print()
        if questionary.confirm(self.t("service.title"), default=True,
                               style=QUESTIONARY_STYLE).ask():
            service.install()  # writes the unit, enables linger, starts — prints its own status
        else:
            console.print(
                "[muted]No problem — start me whenever with "
                "[accent]moko service install[/accent].[/muted]"
            )

    def _summary(self) -> None:
        c = self.config
        now = catalog.format_time_label(Clock(c.timezone).now().strftime("%H:%M"))
        grid = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        grid.add_column(style="muted", justify="right")
        grid.add_column()
        grid.add_row(self.t("summary.name"), f"[accent]{c.companion_name}[/accent]")
        grid.add_row(self.t("summary.language"), i18n.language_name(c.language))
        grid.add_row(self.t("summary.timezone"),
                     f"{c.timezone}  [moko]({self.t('summary.now', time=now)})[/moko]")
        grid.add_row(self.t("summary.sleep"),
                     f"{catalog.format_time_label(c.sleep_start)} → "
                     f"{catalog.format_time_label(c.sleep_end)}")
        grid.add_row(self.t("summary.presence"), self.t(f"presence.{c.proactivity}"))
        grid.add_row(self.t("summary.brain"), catalog.brain_label(c))
        if c.telegram_user_id:
            grid.add_row(self.t("summary.channels"), "Telegram")

        console.print()
        console.print(Panel(
            grid, border_style="moko",
            title=f"[accent]{self.t('summary.title')}[/accent]",
            subtitle=f"[muted]{self.t('summary.saved', path=self.saved_path)}[/muted]",
            padding=(1, 2),
        ))
        console.print(f"\n[moko]{self.t('summary.farewell', name=c.companion_name)}[/moko]\n")


def _times(opts: list[str]) -> list[tuple[str, str]]:
    return [(catalog.format_time_label(t), t) for t in opts]
