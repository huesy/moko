# Security Policy

Moko is a **local-first** personal AI: its mind (the graph, thoughts, config,
and secrets) lives only under `~/.moko/` on the machine it runs on. Nothing
leaves that machine except through actions the companion explicitly authorizes.
Security reports matter — thank you for helping keep Moko trustworthy.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub:

1. Go to the repository's **[Security tab](https://github.com/huesy/moko/security)**.
2. Click **“Report a vulnerability”** (GitHub Private Vulnerability Reporting).
3. Describe the issue, the impact, and a reproduction if you have one.

We aim to acknowledge a report within **7 days** and to keep you updated as we
investigate. Coordinated disclosure is appreciated: give us a reasonable window
to ship a fix before any public write-up.

## What's in scope

Because Moko runs on your own machine with your own credentials, the most useful
reports are about **breaking Moko's own safety design**, for example:

- **Consent-gate bypass** — a way to make a sensitive action (sending to a third
  party, spending money, touching system files, destructive ops) run *without*
  the companion's assent.
- **Command-classifier bypass** — getting a blocklisted / privileged command past
  `tools/shell.py` when it should be refused or gated.
- **SSRF / exfiltration** — `web_fetch` reaching loopback/link-local/metadata
  hosts, or a path to leak the mind or secrets off the machine.
- **Secret disclosure** — a way for the model to read a stored credential it was
  never granted, or for a secret to land in logs / output / the graph.
- **Provenance forgery** — writing a fact into the graph without a real source or
  timestamp.

## Known, documented limitation (not a vulnerability)

Moko reads untrusted content from the world (web pages, files, messages) and, like
any such agent, **cannot fully defend against prompt injection**. That's precisely
why sensitive actions stay behind the consent gate. A demonstration that injected
content *tries* to steer Moko is expected; a demonstration that it **slips a
sensitive action past the gate** is a real finding — please report it.

## Safety floors the tests defend

The gates below are enforced in code with regression tests (see
`apps/agent/tests/test_shell_classifier.py`, `test_tools_fs.py`, `test_tools.py`,
`test_brain.py`, and related suites):

- **Consent gate** on sensitive actions (third-party send, spend, system files,
  destructive ops)
- **Shell classifier** — block / ask / free classification with structural checks
  (env prefixes, path traversal, pipe-to-shell, write targets)
- **SSRF resistance** on `web_fetch` (loopback, link-local, private, metadata)
- **Autonomous research** tool surface limited to web + memory (no shell /
  write / secrets while untrusted content is in play)
- **Secrets** owner-only at rest; values never injected into the graph or logs by
  default paths

See [`MISSION.md`](MISSION.md) for the safety floors this policy protects.
