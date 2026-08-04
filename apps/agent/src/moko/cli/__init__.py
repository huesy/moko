"""The `moko` command.

Tiny, friendly surface for now: `moko setup`, `moko status`, and a bare
`moko` that does the kind thing depending on whether we've met yet.
"""

from __future__ import annotations

import argparse

from .. import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="moko",
        description="Moko — your warm, curious, fluffy companion. 🌸",
    )
    parser.add_argument("--version", action="version", version=f"moko {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="Get Moko set up — friendly and guided.")
    cfgp = sub.add_parser(
        "config",
        help="Change Moko's settings — interactive editor, or `moko config KEY [VALUE]`.",
    )
    cfgp.add_argument("config_args", nargs="*", metavar="KEY [VALUE]",
                      help="A setting to get, or KEY VALUE to set. Omit to open the editor.")
    cfgp.add_argument("--list", "-l", action="store_true", help="List every setting and value.")
    cfgp.add_argument("--restart", action="store_true", help="Restart Moko after setting.")
    sub.add_parser("status", help="See how Moko is configured.")
    sub.add_parser("doctor", help="Scan what's set up, running, or needs attention.")
    sub.add_parser("chat", help="Talk to Moko in the terminal.")
    loginp = sub.add_parser("login", help="Connect/refresh a provider sign-in (e.g. Grok).")
    loginp.add_argument("provider", nargs="?", help="which provider to connect (e.g. xai)")
    sub.add_parser("pair", help="Connect Moko to your Telegram.")
    sub.add_parser("start", help="Bring Moko online on every configured channel.")
    sub.add_parser("journal", help="See recent dreams and unprompted messages.")
    snap = sub.add_parser("snapshot", help="Back up Moko's whole mind now (or --list backups).")
    snap.add_argument("label", nargs="?", help="an optional name for this snapshot")
    snap.add_argument("--list", "-l", action="store_true", help="List existing snapshots.")
    rest = sub.add_parser("restore", help="Restore Moko's mind from a snapshot (stop Moko first).")
    rest.add_argument("name", nargs="?", help="the snapshot to restore (`moko snapshot --list`)")
    rest.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt.")
    sub.add_parser("dream", help="Run a consolidation (dream) right now.")
    out = sub.add_parser("outreach", help="Run Moko's reach-out decision (--force = just send).")
    out.add_argument("--force", action="store_true", help="Skip the deliberation and just send.")
    sub.add_parser("reflect", help="Have Moko form a private thought right now.")
    sub.add_parser("research", help="Have Moko look up one of its own questions now.")
    groom = sub.add_parser("groom", help="Outward maintenance: token, claude CLI, updates.")
    groom.add_argument("--upgrade", action="store_true", help="Apply available Moko updates.")
    upd = sub.add_parser("update", help="Update Moko to the latest release.")
    upd.add_argument("--check", action="store_true", help="Only check; don't install.")
    mon = sub.add_parser("monitor", help="Open the local web monitor (live feed + status).")
    mon.add_argument("--no-open", action="store_true", help="Print the URL, don't open a browser.")
    trust = sub.add_parser("trust", help="View/set per-category tool trust.")
    trust.add_argument("trust_args", nargs="*", help="[category] [allow|ask|deny]")
    sec = sub.add_parser("secret", help="Store API credentials Moko can use.")
    sec.add_argument("action", nargs="?", default="list", choices=["set", "list", "rm"])
    sec.add_argument("name", nargs="?", help="credential name (for set/rm)")
    svc = sub.add_parser("service", help="Install/manage Moko's background service.")
    svc.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["install", "start", "stop", "restart", "status", "logs", "uninstall"],
        help="What to do (default: status).",
    )
    args = parser.parse_args(argv)

    from .. import log

    log.configure()

    try:
        if args.command == "setup":
            from . import setup

            return setup.run()

        if args.command == "config":
            from . import config

            return config.run(args.config_args, list_all=args.list, restart=args.restart)

        if args.command == "status":
            from . import status

            return status.run()

        if args.command == "doctor":
            from . import doctor

            return doctor.run()

        if args.command == "chat":
            from . import chat

            return chat.run()

        if args.command == "login":
            from . import login

            return login.run([args.provider] if args.provider else None)

        if args.command == "pair":
            from . import pair

            return pair.run()

        if args.command == "start":
            from . import start

            return start.run()

        if args.command == "journal":
            from . import journal

            return journal.run()

        if args.command == "snapshot":
            from . import snapshot

            return snapshot.run(args.label, list_all=args.list)

        if args.command == "restore":
            from . import restore

            return restore.run(args.name, assume_yes=args.yes)

        if args.command == "dream":
            from . import dream

            return dream.run()

        if args.command == "outreach":
            from . import outreach

            return outreach.run(force=args.force)

        if args.command == "reflect":
            from . import reflect

            return reflect.run()

        if args.command == "research":
            from . import research

            return research.run()

        if args.command == "groom":
            from . import groom

            return groom.run(upgrade=args.upgrade)

        if args.command == "update":
            from . import update

            return update.run(check_only=args.check)

        if args.command == "monitor":
            from . import monitor

            return monitor.run(open_browser=not args.no_open)

        if args.command == "trust":
            from . import trust

            return trust.run(args.trust_args)

        if args.command == "secret":
            from . import secret

            return secret.run(args.action, args.name)

        if args.command == "service":
            from . import service

            return service.run(args.action)

        # Bare `moko`: show status if we've met, else nudge toward setup.
        from ..config import config as cfg

        if cfg.exists():
            from . import status

            return status.run()

        from ..theme import console

        console.print(
            "Hi, I'm [accent]Moko[/accent] 🌸  "
            "Run [accent]moko setup[/accent] and let's get to know each other."
        )
        return 0
    except KeyboardInterrupt:
        from ..theme import console

        console.print("\n[moko]Bye for now![/moko]")
        return 130
