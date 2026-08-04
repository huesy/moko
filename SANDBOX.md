# SANDBOX.md — Moko runtime containment

> Where Moko *lives*. A ladder of runtime tiers — from a bare host process
> to a hardened systemd unit to a full container — so the companion can
> choose how much of their machine Moko can touch. Design opinion, earned:
> the sandbox is **defense in depth beneath the floors**, never a
> replacement for them.

`MISSION.md` is the referee. This doc governs how the daemon is *contained*;
it must not weaken a single safety floor.

---

## Why this exists (and what it honestly does not do)

Moko has real reach into the host. `tools/shell.py::execute_command` runs
arbitrary commands; `tools/files.py` reads/writes/deletes; the consent gate
(`tools/consent.py`) classes risk and asks before the dangerous ones. That
gate — plus the honesty boundary — is Moko's *primary* protection, and it is
not going anywhere. But two mission facts make a second layer worth building:

- **Floor 4: Moko cannot fully defend against prompt injection.** Content it
  reads from the world is untrusted and can try to steer it. The gate blocks
  the *sensitive* actions, but a bug or a clever injection is a real risk.
- **Small, durable, honest about limits.** A sandbox that bounds the *host
  blast radius* of a hijacked or buggy Moko is cheap insurance for the one
  thing we can't undo — damage to the companion's real machine.

**Be honest about the threat model.** A sandbox:

- ✅ **Bounds host filesystem/process damage** — a runaway `execute_command`
  or a bad `write_file` can only wreck what the sandbox exposes.
- ❌ **Does not hide secrets from Moko.** Moko needs the Telegram token and
  the LLM keys to function; they live where it can read them. Containerizing
  changes nothing about secret-at-rest.
- ❌ **Does not seal network egress.** The daemon *must* reach Telegram,
  Anthropic, OpenAI, xAI. Egress stays open; a hijacked Moko could exfiltrate
  the graph over the very channels it needs. **The only defense against
  exfil is the consent gate on third-party sends** — the sandbox sits
  beneath it, it does not launder it away.

So: the sandbox reduces host blast radius. It is not a data-exfil control.
We state this in the docs and in `moko doctor`, so no one mistakes a
container for a guarantee it can't give.

---

## The tier ladder

Chosen at setup, held in config (`runtime.*`), never hard-coded. Three rungs:

| Tier | `runtime.mode` | Isolation | Deps | M7 host-agency | The mind (SQLite) |
|---|---|---|---|---|---|
| **Plain host** | `host` (unhardened) | none | none | full | native |
| **Hardened host** *(default)* | `host` + `hardened: true` | syscall/path/namespace hardening on the systemd unit | none | full (system paths protected) | native |
| **Container** *(opt-in)* | `docker` \| `podman` | full process/mount/PID namespace | a container engine | inside the container only | native **on Linux only** |

**The default is hardened host, not a container.** The reasoning is the
mission's own: Moko's value includes *acting on the real machine* (M7 — edit
my project, run a command here). A container that genuinely isolates is one
that *can't* do that; a container that restores agency by mounting `$HOME`
provides near-zero isolation. Meanwhile hardened-host gives real
host-blast-radius reduction, **zero new dependencies**, native SQLite
durability, and `journalctl` debugging — by hardening the unit Moko already
writes. The container is an explicit "paranoid tier" for companions who
accept Moko living in a box.

---

## Architecture: a `Runtime` abstraction

Most of the CLI already works across a host↔container boundary for free,
because of two existing seams:

1. **State is one directory.** Everything lives under `~/.moko/`
   (`config/paths.py`). Bind-mount that dir into a container **at the
   identical absolute path** and the daemon's view of disk matches the
   host's.
2. **CLI↔daemon IPC is a Unix socket** at `~/.moko/control.sock`
   (`gateway.py::_control_server`, `cli/_daemon.py`). It lives *inside* the
   bind-mounted dir, so host-side `moko dream` reaches a containerized daemon
   over it with **zero protocol change**.

So we classify every command by how it touches the runtime, and only the
last bucket needs new code:

- **FILE-ONLY** (`config`, `status`, `doctor`, `journal`, `trust`, `secret`,
  `snapshot`/`restore`, `chat`) — operate on `~/.moko/` directly. Work
  identically host-side as long as the home is shared. *No change.*
- **DAEMON-SIGNAL** (`dream`, `outreach`, `reflect`, `research`) — hand an
  action to the live daemon over the socket, else run in-process. Work over
  the shared socket. *One correctness fix — see below.*
- **LIFECYCLE** (`service`, `start`, `login`, `pair`) — manage the OS-level
  runtime or do interactive auth. **These become runtime-aware.**

The new module:

```
apps/agent/src/moko/runtime/
  __init__.py     # Runtime protocol + get_runtime(config) factory
  host.py         # HostRuntime — today's systemd --user behaviour, refactored out of service.py
  container.py    # ContainerRuntime — docker/podman command construction
  hardening.py    # the hardened systemd unit directives (shared by HostRuntime)
```

`Runtime` is a small protocol: `install()`, `start()`, `stop()`,
`restart()`, `status()`, `logs()`, `uninstall()`, `state() -> dict`,
`boot_persist()`. `cli/service.py` and `cli/start.py` shrink to
`get_runtime(cfg).<verb>()`. Behaviour for existing host installs is
byte-for-byte unchanged (HostRuntime *is* the current code, relocated).

Config gains:

```yaml
runtime:
  mode: host            # host | docker | podman
  hardened: true        # host mode: apply the systemd hardening profile
  image: moko:local     # container mode: image tag
  container_name: moko
  monitor_port: 4319    # host port the container publishes the monitor on
  mounts: []            # extra host paths to expose inside the container (each is config)
```

---

## The dual-writer fix (land this first, before any container code)

**The sharpest risk isn't the container — it's the in-process fallback
becoming a second Moko.** Every DAEMON-SIGNAL command calls
`_daemon.try_command()` and, on *any* failure — no socket, a permission
error, or a response slower than the read timeout — returns `None` and falls
back to `asyncio.run(...)` in-process. On the host that's correct: no daemon
means run it yourself. But a containerized daemon that's merely *slow*, or
whose socket the host can't open (Docker rootless UID-remap → EACCES), makes
the host spin up a **second full mind** dreaming and writing into the same
bind-mounted DB — duplicate dreams, double outreach (blowing the proactivity
governor), racing integrity passes. SQLite's locks prevent byte-corruption,
not two minds.

The fix, ~30 lines at one choke point (`cli/_daemon.py` + `cli/_common.py`):

1. **Distinguish "couldn't connect" from "connected but slow."** `_send`
   returns `None` *only* when the connection itself fails; a successful
   connect that then times out returns a "still working…" line (the daemon
   is alive — never fall back).
2. **Make fallback runtime-aware.** When `runtime.mode != host`, a daemon is
   *always* expected (the container *is* the daemon). If it's unreachable,
   **fail loud** ("Moko's container isn't responding — `moko service
   status`") and never fall back. Only `host` mode falls back in-process.
3. **Reap the stale socket.** A crashed container leaves the bind-mounted
   socket behind; `_send` currently only checks `path.exists()`. Treat a
   present-but-unconnectable socket as unreachable, not as "no daemon."

This also fixes a latent *host* bug: a >timeout command today spawns a
duplicate in-process run. Worth landing on its own merit.

---

## Claude Code auth in the container

`llm/claude_oauth.py` reads the OAuth token from `~/.claude/.credentials.json`
— hard-coded, **outside** `~/.moko`, refreshed in place — and shells out to
`claude --version` for the user-agent the Anthropic OAuth endpoint requires.
A container with only `~/.moko` mounted can't see any of that.

**Decision: in container mode, Claude Code lives *inside* the container.**
The container is Moko's own little machine, so it gets its own Claude login:

- The image ships the **standalone `claude` binary** (native installer, not
  the npm package — no Node in a Python/uv image), pinned, updatable via the
  existing `moko groom` path.
- The container user's `~/.claude` is a **persisted volume** (mounted under
  `~/.moko/claude/`), so the login survives image rebuilds — nothing lost on
  `docker rm`.
- **`moko login` becomes runtime-aware.** Host/hardened-host: today's flow,
  reads the host's `~/.claude` — *unchanged*. Container: `moko login` execs
  into the container and runs the Claude login there (code-paste/headless
  flow, so it survives the container boundary); the token lands in the
  mounted volume and Moko reads/refreshes it exactly as now.

Why install the CLI rather than have Moko mint the token itself: the initial
Claude *subscription* OAuth is Claude Code's own undocumented flow. Delegating
the fragile part to the tool that maintains it — and just persisting its
output — is the small/durable choice. Moko's own code barely changes
(`claude_oauth.py` still just reads `~/.claude`, now a mounted path); the only
new logic is `moko login` knowing to exec.

This affects **only** the opt-in container tier. API keys and xAI OAuth
already live in `~/.moko/secrets.yaml`, so they're self-contained in every
tier.

---

## Per-tier specifics

### Hardened host

Add a hardening block to the systemd unit `service.py` already writes, gated
on `runtime.hardened`. Directives (all degrade safely if the kernel/systemd
is old — `moko doctor` reports what took effect):

```ini
NoNewPrivileges=yes
ProtectSystem=strict          # /usr /boot /efi read-only
ProtectHome=tmpfs             # $HOME hidden…
ReadWritePaths=%h/.moko       # …except Moko's own home (+ any runtime.mounts)
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6   # unix socket + IPv4/6 egress; nothing exotic
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
```

Note `ProtectHome=tmpfs` + `ReadWritePaths=%h/.moko` means Moko sees only its
own home by default — but M7 host-agency is preserved by adding the
companion's chosen working directories to `runtime.mounts` → `ReadWritePaths`.
The **default** keeps Moko able to act (system paths protected, `$HOME`
reachable) unless the companion opts into the tighter `ProtectHome=tmpfs`
posture; `moko config runtime.hardened strict` is the knob.

### Container

- **Image, built from source** (`apps/agent/Dockerfile`) — matches
  `install.sh`'s ethos: the machine runs what the repo says. Base image
  **pinned by digest**, deps from the committed `uv.lock` — *that's* the real
  supply-chain boundary, so we pin it and say so. No prebuilt registry image
  (a trust anchor that contradicts "local, inspectable").
- **Install is fully automatic.** With `runtime.mode = podman|docker`,
  `moko service install` **detects the engine** (offering a consented install
  if it's missing — a system package needs root, so it's never a silent sudo),
  **builds the image** (from a local checkout, or straight from git when Moko
  is installed as a tool — `<repo>#<ref>:apps/agent`, so no source tree
  needed), **launches**, and **sets up boot-persistence**. The companion never
  runs `build` by hand. (The file is named `Dockerfile` so both engines
  auto-find it in any context — local or git-subdir — with no `-f`.)
- **Podman preferred over Docker on Linux.** Daemonless, rootless-first, and
  — critically — rootless Podman maps the container UID to the host user, so
  the 0600 control socket and `secrets.yaml` it writes are owned by *you* and
  host-side CLI can open them. Rootless *Docker* remaps to a subuid the host
  user can't open (→ EACCES → the dual-writer trap above). `ContainerRuntime`
  targets both via `runtime.mode` (`podman`|`docker`) selecting the binary;
  the docker path documents the ownership caveat.
- **Mounts:** `~/.moko` → same absolute path (keeps the consent gate's
  workspace path-math honest); `~/.moko/claude` → container `~/.claude`;
  plus each `runtime.mounts` entry. Nothing else from the host.
- **Monitor:** the daemon binds `127.0.0.1` *inside the container's* netns,
  which isn't host loopback. Set `MOKO_MONITOR_HOST=0.0.0.0` in the container
  and publish `-p 127.0.0.1:<port>:4319` so only host loopback reaches it —
  preserving the "local tier = no auth" assumption. Tailscale/network tiers
  need `--network host` or a published port + the existing token auth.
- **Boot persistence differs per backend** and is owned by the Runtime, not
  bolted on: host → `systemctl --user enable` + linger; Podman → a Quadlet /
  `podman generate systemd` unit + linger; Docker → `--restart unless-stopped`
  + the Docker service enabled.
- **Schema-version handshake.** Both the (editable) host `moko` and the built
  image open the same DB. If one runs a newer migration than the other knows,
  the older code reads a DB it doesn't understand. Guard in `store/db.py`:
  after migrating, if the DB's max applied version **exceeds** the highest
  migration the running code ships, **refuse to open** with a clear message
  ("this mind was migrated by a newer Moko — update this one"). Fail closed,
  never silently operate a future schema.

---

## Platform scope

- **Linux:** the full ladder — hardened-host *and* Podman/Docker container.
- **macOS / Windows:** hardened-host-equivalent where the OS allows;
  container **documented but the mind stays host-native**. SQLite on a bind
  mount inside a VM-backed engine (Docker Desktop, WSL2) crosses
  virtiofs/9p, where file-locking and `fsync` durability are historically
  unreliable — an unacceptable corruption risk for the one irreplaceable
  file. We do **not** ship a config that puts `moko.db` on a VM-crossing bind
  mount; that scoping is explicit, not discovered when a graph corrupts.
  (`sandbox-exec`/Seatbelt on macOS is deprecated as of 26.3 — not built on.
  Windows Sandbox is ephemeral — disqualified for a persistent mind.)

---

## Build order (each phase tested + green before the next)

1. **Dual-writer fix** — `_daemon` connect-vs-timeout + runtime-aware
   fallback + stale-socket reap. *(Correctness; independently valuable.)*
2. **`runtime.*` config** — the dataclass, load/save, TUI/CLI editing.
3. **Schema-version guard** — `store/db.py` refuses a future-migrated DB.
4. **`Runtime` abstraction** — extract `HostRuntime` from `service.py`;
   `get_runtime`; `service`/`start` dispatch through it. *No behaviour
   change for existing host installs.*
5. **Hardened-host tier** — the hardening profile + `runtime.hardened` knob;
   `doctor` reports what took effect.
6. **Container tier** — `Dockerfile`, `ContainerRuntime` (podman/docker),
   runtime-aware `login`/`service`/`start`, mounts, monitor publish, Claude
   in-container, boot persistence.

Tests mirror the existing seams: fake subprocess/transport + injectable
clock, unit-test command construction (as `test_service.py` does for
`systemctl`), and assert the fallback *refuses* in container mode.

## CHECKLIST (append to CHECKLIST.md)

- [ ] `_daemon` distinguishes unreachable-daemon from slow-daemon; container
      mode never falls back to a second in-process mind; stale socket reaped.
- [ ] `runtime.{mode,hardened,image,container_name,monitor_port,mounts}` in
      config, round-tripped, editable via `moko config` + TUI, `moko setup`
      offers the tier.
- [ ] `store/db.py` refuses a DB whose applied schema exceeds the code's.
- [ ] `runtime/` module; `HostRuntime` is the relocated systemd behaviour;
      `service`/`start` dispatch through `get_runtime`; host installs
      unchanged (proved by `test_service.py`).
- [ ] Hardened systemd unit behind `runtime.hardened`; degrades safely on old
      systemd; `moko doctor` reports effective hardening.
- [ ] `Dockerfile` (pinned base + `uv.lock`, ships `claude` binary);
      `ContainerRuntime` builds correct podman/docker argv for
      install/start/stop/restart/status/logs/uninstall + boot persistence.
- [ ] `moko login` runtime-aware (execs into container for Claude);
      `~/.moko/claude` persisted; host mode unchanged.
- [ ] Monitor reachable from host loopback under container mode; local tier
      stays unauthenticated only via loopback publish.
- [ ] Threat-model honesty surfaced in `moko doctor` (bounds host damage;
      does not seal egress or hide secrets).
