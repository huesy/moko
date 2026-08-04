"""Deterministic risk classifier for shell commands — the safety floor in front
of the one general command tool.

The posture is **trusted-operator on their own machine** (the deployment Moko is
built for): the companion has handed Moko their box and asked it to act. So the
gate doesn't try to be an OS sandbox — it can't be one without an OS sandbox, and
pretending (gating `python3 -c` while a `node app.js` two lines down can do the
same thing) is theatre. Instead it gates the things a careful person actually
wants a heads-up on, and frees the rest:

  - **Refused outright** (non-overridable): the catastrophes that are never a
    real request (rm -rf / or ~, fork bombs, mkfs, raw-disk dd, piping a download
    straight into a shell) and reading/writing the crown-jewel credential files.
  - **Gated** (ask first; some learnable via "always"): the genuine boundary
    crossings — touching the *system* (sudo, package managers, systemd), reaching
    the *network* (downloads, installs, remote shells, sending data off-box), and
    writing or deleting *outside* Moko's own work folder.
  - **Free**: everything bounded to the workspace — reads, builds, tests, dev
    servers, `python3 -c`, heredocs, in-workspace writes/deletes. Moko's own space
    is Moko's to manage.

How it works:
  1. Heredoc bodies are stripped first (they're *data* written to a file, not
     commands), then a hardline **blocklist** scans the raw skeleton.
  2. The command is split on shell operators (`&& || ; | & \\n`) into subcommands;
     **every** subcommand is classified and the *severest* tier wins. `bash -c
     '<shell>'` is classified by recursing on the inner shell.
  3. Command substitution / eval / base64-decode can hide anything, so they
     escalate the whole command to at least GATE.
  4. Each gated action carries a **key** — the boundary class it crosses. A
     standing "trust this key" (set when the companion picks "always") frees that
     class, but never privilege, destruction, the blocklist, or an unknown command.

This stops Moko's foot-guns and the obvious catastrophes; it is honest guardrails,
not a formal boundary.
"""

from __future__ import annotations

import platform
import re
import shlex
from pathlib import Path

from ..config.paths import moko_home
from .base import GateDecision
from .consent import ALLOW, ASK, DENY

# ── tiers, severest last (higher rank = more dangerous) ───────────────────────
FREE = "free"  # read / local compute / anything bounded to the workspace → run
GATE = "gate"  # system, network, or out-of-workspace fs → ask (some learnable)
PRIVILEGED = "privileged"  # sudo / system packages / system paths → ask (never auto)
DESTRUCTIVE = "destructive"  # data loss outside the workspace / exfiltration / remote → ask
BLOCKED = "blocked"  # catastrophe → deny, non-overridable

_RANK = {FREE: 0, GATE: 1, PRIVILEGED: 2, DESTRUCTIVE: 3, BLOCKED: 4}

# The GATE-tier boundary classes the companion can trust with "always" (each frees
# that whole class, nothing more). PRIVILEGED/DESTRUCTIVE keys are never freeable —
# escalation always asks — and an unknown command carries no key, so it always asks.
FREEABLE_KEYS = frozenset({"network", "install", "runner", "signal", "outside_write"})


def workspace() -> Path:
    """Moko's work root: where shell commands run by default. A sandbox-by-
    convention that bounds the blast radius of writes/deletes — not real isolation,
    but it keeps everyday work off the rest of the machine and the ~/.moko dir tidy."""
    root = moko_home() / "work"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── the workspace boundary (Moko's own space is free; outside it is gated) ─────
def _expand(path: str) -> str:
    """Substitute $HOME / ${HOME} (expanduser handles ~)."""
    home = str(Path.home())
    return path.replace("${HOME}", home).replace("$HOME", home)


# Pseudo-files that aren't real writes — redirecting/pointing at them is harmless,
# so they never count as "outside the workspace".
_DEV_SINKS = {"/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom",
              "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty"}


def _is_dev_sink(token: str) -> bool:
    t = _expand(token).strip()
    return t in _DEV_SINKS or t.startswith("/dev/fd/")


def _resolved(token: str) -> Path | None:
    """Resolve a path token to an absolute path for the workspace-boundary check.
    A *relative* path anchors to the workspace (shell commands run there) — not to
    the daemon's CWD — and `~`/`$HOME` are expanded. `resolve()` collapses `..` and
    symlinks, so a traversal like `../../etc` can't pose as in-workspace. None if
    it can't be resolved."""
    raw = _expand(token).strip()
    if not raw:
        return None
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = workspace() / p
        return p.resolve()
    except (ValueError, OSError, RuntimeError):
        return None


def in_workspace(path: str) -> bool:
    """True if `path` is inside Moko's work folder — its own to manage freely.
    Relative paths are workspace-relative; absolute/`~`/`$HOME` paths must resolve
    under the work root. Resolution collapses `..`, so a relative traversal out of
    the workspace is correctly seen as *outside* (was a write-gate bypass)."""
    if _is_dev_sink(path):
        return True
    rp = _resolved(path)
    if rp is None:
        return False
    try:
        ws = workspace().resolve()
    except (OSError, RuntimeError):
        return False
    return rp == ws or ws in rp.parents


# Targets that ARE `/` or the home dir (or sweep everything under them) — never
# legitimate, and distinct from a deep path *under* the workspace.
_ROOT_HOME = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*",
              "${HOME}", "${HOME}/", "${HOME}/*"}


def _is_root_or_home(token: str) -> bool:
    tok = token.strip()
    if tok in _ROOT_HOME or _expand(tok).rstrip("/") in {"", "/", str(Path.home())}:
        return True
    rp = _resolved(tok)  # also catch a traversal that *resolves* to / or home
    return rp is not None and str(rp) in {"/", str(Path.home())}


def _is_outside(token: str) -> bool:
    """A path NOT inside the workspace (and not a /dev sink). Relative paths
    resolve against the workspace, so `../escape` is correctly outside."""
    if _is_dev_sink(token):
        return False
    return not in_workspace(token)


# ── secret handling for the shell (least privilege + redaction) ───────────────
_VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def referenced_secret_env(command: str) -> dict[str, str]:
    """The stored secrets a command explicitly references by $NAME — and ONLY
    those. A skill can use a credential (`curl -H "key: $ELEVENLABS_API_KEY"`)
    without Moko ever seeing the value, and a command that doesn't name a secret
    gets none of them (least privilege)."""
    from ..config.secrets import all_secrets

    secrets = all_secrets()
    if not secrets:
        return {}
    named = set(_VAR_REF.findall(command))
    return {k: v for k, v in secrets.items() if k in named}


def redact_secrets(text: str) -> str:
    """Replace any stored secret value with *** before the LLM sees the output —
    the backstop so a command that prints a credential can't leak it to Moko."""
    from ..config.secrets import all_secrets

    for value in all_secrets().values():
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    return text


# ── (1) hardline blocklist — never run, regardless of policy ──────────────────
# Matched on the raw, whitespace-normalized skeleton (heredoc bodies already
# stripped). Deliberately narrow: only things that are never a legitimate request.
# (rm of / or ~ is caught precisely in the per-command classifier via tokens — a
# regex over-matched deep paths like `rm -rf ~/.moko/work/site`.)
_BLOCKLIST: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bdd\b[^|&;]*\bof=/dev/(sd|nvme|disk|hd|mmcblk|vd)"),
     "writing a raw disk device"),
    (re.compile(r"\bmkfs\b"), "formatting a filesystem"),
    (re.compile(r">\s*/dev/(sd|nvme|disk|hd|mmcblk|vd)"), "writing a raw disk device"),
    (re.compile(r"\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"),
     "piping a downloaded script straight into a shell"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(python|perl|ruby|node)\b"),
     "piping a download straight into an interpreter"),
)

# Obfuscation: we can't see inside these, so they escalate (≥ GATE) — command
# substitution and eval/base64 can hide a worse command than the tokens reveal.
_OBFUSCATION = re.compile(r"`|\$\(|<\(|\beval\b|\bbase64\s+(-d|--decode)\b")

# The crown jewels — one list, shared by the shell AND the file tools, so a deny
# can't be routed around (a per-tool list that drifts is "unpaired theater"). Moko
# uses the secrets store for credentials it needs; it never reads these directly.
_SECRET_MARKERS = (
    "/.ssh", "/.gnupg", "/.aws", "/.config/gcloud", "/.moko/secrets",
    "id_rsa", "id_ed25519", "id_ecdsa", ".pem", ".key", ".p12", ".netrc",
)


def is_secret_path(path: str) -> bool:
    """True if a path is an unambiguous credential/key store Moko must not read or
    write. Deliberately narrow: `.env` isn't here — it's a normal project file, so
    it follows the workspace boundary like anything else (real credentials Moko
    needs go through the secrets store, never read off disk)."""
    return any(marker in path.lower() for marker in _SECRET_MARKERS)


# Shell operators that separate subcommands.
_OPERATORS = {"&&", "||", ";", "|", "|&", "&", "\n"}

# Output redirects — a write whose risk is the *target's*, not the command's.
_REDIRECTS = {">", ">>", "1>", "2>", "&>", ">|", "0>", "2>>"}

# Benign leading wrappers we strip to classify the *real* command underneath.
_STRIP_WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf", "ionice"}


# ── heredocs: the body is data, not commands — strip it before classifying ─────
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(command: str) -> str:
    """Remove heredoc bodies (`<< EOF … EOF`), leaving the command skeleton. The
    body is content written to a file, never executed — lexing it as shell is how
    `cat > f << EOF …html… EOF` used to trip on a CSS line. The redirect target on
    the opening line is preserved, so writing the heredoc *outside* the workspace
    still gates."""
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC.search(line)
        i += 1
        if m:
            delim = m.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1  # drop the closing delimiter line too
    return "\n".join(out)


# ── per-command tables ────────────────────────────────────────────────────────
_FREE_EXE = frozenset({
    # read / inspect
    "ls", "pwd", "echo", "printf", "cat", "bat", "head", "tail", "nl", "tac", "rev",
    "wc", "sort", "uniq", "cut", "tr", "column", "fold", "join", "paste",
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "awk", "jq", "yq", "xxd", "od",
    "strings", "file", "stat", "du", "df", "basename", "dirname", "realpath",
    "readlink", "which", "type", "command", "whoami", "id", "groups", "hostname",
    "uname", "arch", "date", "cal", "uptime", "free", "ps", "printenv",
    "true", "false", "test", "seq", "clear", "tput", "tree", "diff", "cmp", "comm",
    "md5sum", "sha1sum", "sha256sum", "expr", "bc", "locale", "tldr", "man", "help",
    # local dev: build / test / lint / typecheck (NOT install — see installers)
    "make", "tsc", "vite", "webpack", "rollup", "esbuild", "eslint", "prettier",
    "pytest", "jest", "vitest", "mocha", "ruff", "mypy", "black", "isort", "flake8",
    "tox", "rustc", "gcc", "g++", "clang", "javac", "dotnet",
    # harmless shell builtins (navigation / no side effects beyond the subshell)
    "cd", "pushd", "popd", "export", "unset", "alias", "unalias", "set", ":",
})

# Privilege escalation / system-level → always ASK.
_PRIVILEGED_EXE = frozenset({
    "sudo", "su", "doas", "pkexec",
    "apt", "apt-get", "aptitude", "dpkg", "dnf", "yum", "rpm", "pacman", "zypper",
    "snap", "flatpak", "brew", "port", "emerge",
    "systemctl", "service", "systemd-run", "launchctl", "mount", "umount",
    "useradd", "userdel", "usermod", "groupadd", "visudo", "chsh", "passwd",
    "shutdown", "reboot", "halt", "poweroff", "init", "modprobe", "insmod",
    "iptables", "ufw", "nft", "setcap", "chroot", "chown", "chgrp",
})

# Remote / exfiltration → always ASK.
_REMOTE_EXE = frozenset({"ssh", "scp", "sftp", "telnet", "nc", "ncat", "socat"})

# Package installers — arbitrary code via postinstall/build scripts → GATE.
_INSTALLER_EXE = frozenset({"pipx", "gem", "composer", "bundle", "poetry"})

# Network fetchers (could download an executable) → GATE; data-upload → DESTRUCTIVE.
_FETCH_EXE = frozenset({"curl", "wget", "fetch", "http", "https", "aria2c"})

# Inline-code interpreters: a script *file* is everyday dev (free); `-c`/`-e` is
# opaque, but on a trusted operator's box it's no more dangerous than running a
# script file, so it's free too. (`bash -c` is special-cased to recurse.)
_INTERPRETER_EXE = frozenset({"python", "python3", "ruby", "perl", "php", "Rscript",
                              "node", "deno", "lua"})

# Shells that execute their stdin as commands when handed no script file — the
# dangerous tail of a `curl … | sh` pipeline, however many filters sit between.
_SHELL_EXE = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_STDIN_INTERP = _SHELL_EXE | _INTERPRETER_EXE

# Commands whose risk is whether their target is *outside* the workspace.
_WRITE_EXE = frozenset({"cp", "mv", "ln", "rsync", "install", "tee", "truncate",
                        "shred", "tar", "zip", "unzip", "mkdir", "touch", "chmod"})


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Peel benign wrappers (timeout 30, nice -n 5, nohup …) so the real command
    is what gets classified. Command-*runners* (npx, docker, xargs, find -exec) are
    NOT peeled — they're the bypass surface and get classified on their own terms."""
    i = 0
    while i < len(tokens):
        head = _basename(tokens[i])
        if head not in _STRIP_WRAPPERS:
            break
        i += 1
        # skip the wrapper's own option args (e.g. `nice -n 5`, `timeout 30`)
        while i < len(tokens) and (tokens[i].startswith("-") or tokens[i].isdigit()):
            i += 1
    return tokens[i:]


def _basename(token: str) -> str:
    """The executable name without path or assignment prefix (`/usr/bin/rm` → rm)."""
    if "=" in token and "/" not in token.split("=", 1)[0]:
        return ""  # a leading VAR=value env assignment, not a command
    return token.rsplit("/", 1)[-1]


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    """Peel leading `VAR=value` env assignments so the *real* command underneath
    gets classified — `FOO=1 rm -rf /` is an rm, not a free no-op. (The old code
    saw the empty basename of `FOO=1` and returned FREE, discarding the rest: a
    full classifier bypass.)"""
    i = 0
    while i < len(tokens) and _ASSIGN.match(tokens[i]) and "/" not in tokens[i].split("=", 1)[0]:
        i += 1
    return tokens[i:]


def _write_targets(exe: str, args: list[str]) -> list[str]:
    """The paths a write-family command actually writes to (so reading *from*
    outside the workspace doesn't gate — only writing *to* outside does)."""
    nonflag = [a for a in args if not a.startswith("-")]
    if exe in {"cp", "mv", "ln", "rsync", "install"}:
        # `-t DIR` / `--target-directory=DIR`: the destination is the flag's
        # argument and every operand is a *source*. Without this, `cp -t /etc x`
        # treats /etc as a source and the in-workspace x as the destination —
        # writing into /etc while classifying it as a free in-workspace write.
        for i, a in enumerate(args):
            if a in {"-t", "--target-directory"} and i + 1 < len(args):
                return [args[i + 1]]
            if a.startswith("--target-directory="):
                return [a.split("=", 1)[1]]
        return nonflag[-1:]  # the destination is the last operand
    if exe == "sed":
        return nonflag[1:]  # the first non-flag operand is the script expression
    return nonflag  # tee/truncate/shred/mkdir/touch/chmod/tar/zip/unzip


def _git_tier(args: list[str]) -> tuple[str, str, str]:
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub == "push" and any(a in {"-f", "--force"} or a.startswith("--force") for a in args):
        return DESTRUCTIVE, "git force-push (rewrites remote history)", "egress"
    if sub in {"clone", "pull"}:
        return GATE, f"git {sub} fetches code from a remote", "network"
    if sub == "push":
        return GATE, "git push sends commits to a remote", "network"
    return FREE, "", ""  # status/log/diff/add/commit/checkout/merge/reset/stash/… — local


def _pkg_tier(exe: str, args: list[str]) -> tuple[str, str, str]:
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub in {"install", "add", "ci", "i", "update", "upgrade", "global", "create", "dlx"}:
        return GATE, f"{exe} {sub} (runs package scripts / fetches code)", "install"
    return FREE, "", ""  # run/build/test/start/lint/dev/audit/list/… — local dev


def _classify_subcommand(tokens: list[str]) -> tuple[str, str, str]:
    """Tier one subcommand (already split off and wrapper-stripped). Returns
    (tier, reason, key) — `key` is the boundary class for "always", "" for FREE."""
    tokens = _strip_env_assignments(_strip_wrappers(tokens))
    if not tokens:
        return FREE, "", ""  # only wrappers / bare VAR=value assignments — nothing runs
    exe = _basename(tokens[0])
    args = tokens[1:]
    if not exe:  # an unidentifiable leading token — fail closed rather than free
        return GATE, "couldn't identify the command", ""

    if exe == "env":
        return _classify_env(args)  # env runs the command after it — classify that
    if exe in _PRIVILEGED_EXE:
        return PRIVILEGED, f"{exe} needs system privileges", "system"
    if exe in _REMOTE_EXE:
        return DESTRUCTIVE, f"{exe} reaches another machine", "remote"
    if exe in {"bash", "sh", "zsh", "dash", "ksh"}:
        inline = _inline_code(args)
        if inline is not None:
            return classify_posix(inline)  # recurse: classify the inner shell itself
        return FREE, "", ""  # running a script file
    if exe == "git":
        return _git_tier(args)
    if exe in {"npm", "pnpm", "yarn", "bun", "cargo", "go", "pip", "pip3", "uv", "deno"}:
        if exe in {"pip", "pip3"} or (exe == "uv" and "pip" in args):
            if "install" in args or "uninstall" in args:
                return GATE, f"{exe} install", "install"
            return FREE, "", ""
        return _pkg_tier(exe, args)
    if exe in _INSTALLER_EXE:
        return GATE, f"{exe} installs software", "install"
    if exe in {"npx", "pnpx", "bunx", "docker", "podman", "docker-compose", "kubectl",
               "vagrant", "ansible", "terraform", "helm", "devbox", "mise", "direnv"}:
        return GATE, f"{exe} runs other programs", "runner"  # never freed implicitly
    if exe in _FETCH_EXE:
        if any(a in {"-d", "--data", "--data-binary", "--data-raw", "-T", "--upload-file",
                     "-F", "--form", "-X"} or a.upper() in {"POST", "PUT"} for a in args):
            return DESTRUCTIVE, f"{exe} uploads/sends data off the machine", "egress"
        return GATE, f"{exe} downloads from the network", "network"
    if exe == "rm":
        targets = [a for a in args if not a.startswith("-")]
        if any(_is_root_or_home(t) for t in targets):
            return BLOCKED, "deletes / or your home directory", ""
        if any(_is_outside(t) for t in targets):
            return DESTRUCTIVE, "deletes paths outside the workspace", "outside_delete"
        return FREE, "", ""  # inside Moko's own workspace
    if exe == "dd":
        return DESTRUCTIVE, "dd can overwrite devices/files wholesale", "outside_write"
    if exe in {"kill", "pkill", "killall"}:
        return GATE, f"{exe} signals processes", "signal"
    if exe == "find":  # read-only unless it acts (-exec/-delete)
        if any(a in {"-exec", "-execdir", "-delete", "-fls", "-fprint"} for a in args):
            return GATE, "find -exec/-delete acts on what it finds", "runner"
        return FREE, "", ""
    if exe == "sed" and any(a.startswith("-i") for a in args):
        if any(_is_outside(t) for t in _write_targets(exe, args)):
            return GATE, "sed -i edits a file outside the workspace", "outside_write"
        return FREE, "", ""
    if exe in _WRITE_EXE:
        if any(_is_outside(t) for t in _write_targets(exe, args)):
            return GATE, f"{exe} writes outside the workspace", "outside_write"
        return FREE, "", ""
    if exe in _INTERPRETER_EXE:
        return FREE, "", ""  # script file or inline code — bounded like everything else
    if exe in _FREE_EXE:
        return FREE, "", ""

    return GATE, f"'{exe}' isn't on the known-safe list", ""  # fail closed, not freeable


def _classify_env(args: list[str]) -> tuple[str, str, str]:
    """`env` runs the command that follows it, so classify *that*, not `env`. Peels
    env's own flags and VAR=value pairs; `-S/--split-string` carries a command
    string, which is split and classified. `env` alone just prints/sets the
    environment (free). Without this, `env sudo reboot` / `env -i bash -c …`
    classified FREE."""
    i = 0
    while i < len(args):
        t = args[i]
        if t == "--":
            i += 1
            break
        if t in {"-S", "--split-string"} and i + 1 < len(args):
            try:
                inner = shlex.split(args[i + 1])
            except ValueError:
                return GATE, "env -S carries a command we couldn't parse", ""
            return _classify_subcommand(inner) if inner else (FREE, "", "")
        if t.startswith("--split-string="):
            try:
                inner = shlex.split(t.split("=", 1)[1])
            except ValueError:
                return GATE, "env -S carries a command we couldn't parse", ""
            return _classify_subcommand(inner) if inner else (FREE, "", "")
        if t in {"-u", "--unset", "-C", "--chdir"} and i + 1 < len(args):
            i += 2  # flag + its argument
            continue
        if t.startswith("-"):
            i += 1
            continue
        if _ASSIGN.match(t) and "/" not in t.split("=", 1)[0]:
            i += 1  # VAR=value
            continue
        break
    rest = args[i:]
    return _classify_subcommand(rest) if rest else (FREE, "", "")


def _inline_code(args: list[str]) -> str | None:
    """The inline program a `-c`/`-e`/`--eval` invocation carries, if any."""
    for i, a in enumerate(args):
        if a in {"-c", "-e", "--eval"} and i + 1 < len(args):
            return args[i + 1]
    return None


def _reads_stdin_as_code(tokens: list[str]) -> bool:
    """True if this pipeline stage would execute piped stdin as code: a shell or
    interpreter given no script *file* (and no inline `-c`/`-e`, which the
    recursion classifies on its own) reads and runs its stdin."""
    if not tokens:
        return False
    exe = _basename(tokens[0])
    if exe not in _STDIN_INTERP:
        return False
    args = tokens[1:]
    if _inline_code(args) is not None:
        return False  # running the given inline code, not the piped stream
    for a in args:
        if a == "-":
            return True  # explicit "read from stdin"
        if not a.startswith("-"):
            return False  # a script-file operand → runs the file, not stdin
    return True


_PIPE_OPS = {"|", "|&"}


def _pipelines(command: str) -> list[list[list[str]]] | None:
    """Tokenize (respecting quotes) and group into pipelines: stages joined by `|`
    are one pipeline; `; && || & \\n` start a new one. Redirects stay with their
    stage. None if it can't be parsed (caller fails closed → gate)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    pipelines: list[list[list[str]]] = []
    pipe: list[list[str]] = []
    cur: list[str] = []

    def end_stage() -> None:
        nonlocal cur
        if cur:
            pipe.append(cur)
            cur = []

    def end_pipe() -> None:
        nonlocal pipe
        end_stage()
        if pipe:
            pipelines.append(pipe)
            pipe = []

    for tok in tokens:
        if tok in _PIPE_OPS:
            end_stage()  # boundary *within* a pipeline (data flows across it)
        elif tok in _OPERATORS or (set(tok) <= {"&", ";"} and tok):
            end_pipe()  # sequence boundary — a fresh pipeline
        elif tok in {">", ">>", "<", "<<", "<<<", "2>", "&>"}:
            cur.append(tok)  # keep redirects with their stage
        else:
            cur.append(tok)
    end_pipe()
    return pipelines


def _pipeline_downloads_into_interpreter(pipelines: list[list[list[str]]]) -> bool:
    """A network fetch feeding — directly or through ANY intervening filter — a
    shell/interpreter that runs its stdin: `curl x | sh`, `curl x | grep . | sh`,
    `wget -qO- x | tac | bash`. Structural, so a filter can't disguise the intent
    the way the brittle blocklist regex (shell first after the pipe) could be."""
    for pipe in pipelines:
        seen_fetch = False
        for stage in pipe:
            stage = _strip_env_assignments(_strip_wrappers(stage))
            if not stage:
                continue
            if seen_fetch and _reads_stdin_as_code(stage):
                return True
            if _basename(stage[0]) in _FETCH_EXE:
                seen_fetch = True
    return False


def _redirect_tier(tokens: list[str]) -> tuple[str, str, str] | None:
    """A write whose risk is the redirect *target's*: into the workspace (or a /dev
    sink) is free, onto / or home is catastrophic, elsewhere gates."""
    for i, tok in enumerate(tokens[:-1]):
        if tok in _REDIRECTS:
            target = tokens[i + 1]
            if _is_root_or_home(target):
                return BLOCKED, "overwrites / or your home directory", ""
            if _is_outside(target):
                return GATE, f"writes {target} (outside the workspace)", "outside_write"
    return None


def classify_posix(command: str) -> tuple[str, str, str]:
    """Tier a whole POSIX shell command line. Returns (tier, reason, key)."""
    skeleton = _strip_heredocs(command)
    raw = " ".join(skeleton.split())  # normalize whitespace for pattern scans
    for pat, why in _BLOCKLIST:
        if pat.search(raw):
            return BLOCKED, why, ""
    if is_secret_path(raw):
        return BLOCKED, "touches a credential/key file (off-limits — use the secrets store)", ""

    pipelines = _pipelines(skeleton)
    if pipelines is None:
        return GATE, "couldn't parse the command safely", ""
    if _pipeline_downloads_into_interpreter(pipelines):
        return BLOCKED, "piping a downloaded script straight into a shell", ""

    worst, reason, key = FREE, "", ""
    for pipe in pipelines:
        for sub in pipe:
            tier, why, k = _classify_subcommand(sub)
            redir = _redirect_tier(sub)  # writing outside the workspace escalates
            if redir and _RANK[redir[0]] > _RANK[tier]:
                tier, why, k = redir
            if _RANK[tier] > _RANK[worst]:
                worst, reason, key = tier, why, k

    # Obfuscation can hide a worse command than we can see — escalate to ≥ GATE.
    if _OBFUSCATION.search(raw) and _RANK[worst] < _RANK[GATE]:
        return GATE, "uses command substitution / eval we can't inspect", ""
    return worst, reason, key


def _verdict_for(tier: str, key: str, policy: dict[str, str] | None) -> str:
    """Tier + key → consent verdict. A standing `shell: deny` is a global off
    switch; otherwise a trusted *key* frees its GATE-tier class, but privilege,
    destruction, the blocklist, and unknown commands always ask (or deny)."""
    pol = policy or {}
    if tier == BLOCKED:
        return DENY
    if pol.get("shell") == "deny":
        return DENY
    if tier in (PRIVILEGED, DESTRUCTIVE):
        return ASK  # escalation always asks, even when something is trusted
    if tier == GATE:
        if key in FREEABLE_KEYS and pol.get(key) == "allow":
            return ALLOW
        return ASK
    return ALLOW  # FREE


def classify(args: dict, policy: dict[str, str] | None = None) -> GateDecision:
    """The gate's per-call entry for the shell tool: classify the command and
    return a consent verdict + reason + tier + trust key."""
    command = str(args.get("command") or "").strip()
    if not command:
        return GateDecision(ALLOW, "", FREE)
    if platform.system() == "Windows":
        # The Windows (PowerShell/cmd) classifier isn't built yet; until it is,
        # gate everything rather than guess. (Cross-OS is a per-shell parser.)
        return GateDecision(ASK, "Windows command review not implemented yet — asking first", GATE)
    tier, reason, key = classify_posix(command)
    return GateDecision(_verdict_for(tier, key, policy), reason, tier, key)
