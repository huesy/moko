#!/bin/sh
# Moko installer — bring Moko home. 🌸
#
#   curl -fsSL https://mokomoko.ai/install.sh | sh
#
# What it does, and nothing more:
#   1. Installs `uv` (Astral's Python tool manager) if you don't have it.
#   2. Installs the `moko` command — uv fetches an isolated, managed
#      Python 3.12 for it, so you need nothing pre-installed.
#   3. Tells you to run `moko setup`.
#
# It does NOT run as root, touch system files, or install system packages.
# Everything lands under your user directory. Moko's mind never leaves your
# machine.
#
# Read before you pipe to a shell — this is the whole script.
set -eu

# Where Moko's source lives. The repo is public, so anonymous HTTPS works for
# everyone; we probe SSH first only so contributors with keys use them, and fall
# straight back to HTTPS when there's no key. Set MOKO_SLUG for a fork,
# MOKO_REPO_SSH / MOKO_REPO_HTTPS to override a URL, or MOKO_REPO to force one
# exact URL and skip the SSH→HTTPS choice.
MOKO_SLUG="${MOKO_SLUG:-huesy/moko}"
MOKO_REPO_SSH="${MOKO_REPO_SSH:-ssh://git@github.com/${MOKO_SLUG}.git}"
MOKO_REPO_HTTPS="${MOKO_REPO_HTTPS:-https://github.com/${MOKO_SLUG}.git}"
MOKO_REPO="${MOKO_REPO:-}"
MOKO_PYTHON="${MOKO_PYTHON:-3.12}"
# By default we install the latest published release. Pin one with
# MOKO_VERSION=v1.2.3 (or MOKO_REF for any branch / tag / commit).
MOKO_REF="${MOKO_REF:-${MOKO_VERSION:-}}"

# ---- pretty output (only when attached to a terminal) -----------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  PINK=$(printf '\033[38;5;211m'); DIM=$(printf '\033[2m')
  BOLD=$(printf '\033[1m'); RESET=$(printf '\033[0m')
else
  PINK=''; DIM=''; BOLD=''; RESET=''
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s🌸 %s%s\n' "$PINK" "$*" "$RESET"; }
note() { printf '%s   %s%s\n' "$DIM" "$*" "$RESET"; }
die()  { printf '\n%serror:%s %s\n' "$BOLD" "$RESET" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

printf '\n%s%s  Moko%s — your warm, curious, fluffy companion\n\n' "$PINK" "$BOLD" "$RESET"

# ---- 1. ensure uv -----------------------------------------------------------
if have uv; then
  step "uv is already installed ($(uv --version 2>/dev/null || echo present))."
else
  step "Installing uv (Moko's Python toolchain)…"
  have curl || die "curl is required to bootstrap uv. Install curl and re-run."
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
    || die "uv install failed. See https://docs.astral.sh/uv/ and re-run."
  # uv lands in ~/.local/bin (or $XDG_BIN_HOME / $CARGO_HOME/bin); add for now.
  for d in "$HOME/.local/bin" "${XDG_BIN_HOME:-}" "${CARGO_HOME:-$HOME/.cargo}/bin"; do
    [ -n "$d" ] && [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac
  done
  export PATH
  have uv || die "uv installed but isn't on PATH yet. Open a new terminal and re-run."
fi

# ---- 2. pick the version to install ----------------------------------------
# No pin? Ask GitHub for the latest published release. Fall back to main only
# when there are no releases yet (curl fails / none published → empty → main).
if [ -z "$MOKO_REF" ]; then
  MOKO_REF=$(curl -fsSL "https://api.github.com/repos/${MOKO_SLUG}/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
fi
# Still nothing (the API can rate-limit unauthenticated calls)? Try gh if signed in.
if [ -z "$MOKO_REF" ] && have gh; then
  MOKO_REF=$(gh release view --repo "$MOKO_SLUG" --json tagName --jq .tagName 2>/dev/null || true)
fi
if [ -z "$MOKO_REF" ]; then
  MOKO_REF="main"
  RELEASE_NOTE="no reachable release — installing the latest development build (main)"
else
  RELEASE_NOTE="installing release ${MOKO_REF}"
fi

# ---- 3. install the moko command -------------------------------------------
step "Installing Moko…"
note "$RELEASE_NOTE"

# The transport env for any git fetch: SSH-first + fail-fast (BatchMode + a short
# timeout), and no interactive credential prompt. uv shells out to `git`, so these
# carry into a source build below; they're harmless when installing a wheel.
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5"
export GIT_TERMINAL_PROMPT=0

# Build Moko from the git source — the dev / no-release path. The dashboard UI is a
# build artifact and is NOT in the source tree, so a source build ships WITHOUT the
# dashboard; build it with scripts/build-monitor.sh from a clone if you want it.
install_from_source() {
  if [ -n "$MOKO_REPO" ]; then
    _url="$MOKO_REPO"                                      # forced, exact URL
  elif have git && git ls-remote "$MOKO_REPO_SSH" HEAD >/dev/null 2>&1; then
    _url="$MOKO_REPO_SSH"; note "building from source over SSH"
  else
    _url="$MOKO_REPO_HTTPS"; note "building from source over HTTPS"
  fi
  uv tool install --force --python "$MOKO_PYTHON" \
    --from "git+${_url}@${MOKO_REF}#subdirectory=apps/agent" moko
}

installed=""
# A real release? Install its prebuilt WHEEL — it carries the built dashboard and
# needs no Node. Try `gh` first (handles auth / API rate limits); else a plain
# download over HTTPS.
if [ "$MOKO_REF" != "main" ]; then
  tmpw=$(mktemp -d)
  if have gh && gh release download "$MOKO_REF" --repo "$MOKO_SLUG" \
       --pattern '*.whl' --dir "$tmpw" >/dev/null 2>&1; then
    whl=$(ls "$tmpw"/*.whl 2>/dev/null | head -1)
    if [ -n "$whl" ]; then
      note "installing the $MOKO_REF release wheel (dashboard included)"
      if uv tool install --force --python "$MOKO_PYTHON" "$whl"; then installed=1; fi
    fi
  fi
  if [ -z "$installed" ] && have curl; then
    # uv requires a PEP 427 wheel *filename* (name-version-…whl). Saving as
    # bare "moko.whl" makes `uv tool install` reject it and fall back to source.
    url=$(curl -fsSL "https://api.github.com/repos/${MOKO_SLUG}/releases/tags/${MOKO_REF}" \
      2>/dev/null | sed -n 's/.*"browser_download_url": *"\([^"]*\.whl\)".*/\1/p' | head -1)
    if [ -n "$url" ]; then
      whl_name=$(basename "$url")
      case "$whl_name" in *.whl) ;; *) whl_name="moko-0-py3-none-any.whl" ;; esac
      if curl -fsSL "$url" -o "$tmpw/$whl_name" 2>/dev/null; then
        note "installing the $MOKO_REF release wheel (dashboard included)"
        if uv tool install --force --python "$MOKO_PYTHON" "$tmpw/$whl_name"; then installed=1; fi
      fi
    fi
  fi
  if [ -z "$installed" ]; then
    note "couldn't fetch a release wheel for $MOKO_REF — building from source instead"
  fi
fi

if [ -z "$installed" ]; then
  install_from_source \
    || die "Moko install failed — couldn't reach github.com/${MOKO_SLUG}. Check your network and try again, or set MOKO_REPO to an exact clone URL."
fi

# Make sure the tool dir is on PATH for the messaging below.
uv tool update-shell >/dev/null 2>&1 || true

# ---- 4. done ----------------------------------------------------------------
printf '\n%s%s  Moko is installed.%s\n\n' "$PINK" "$BOLD" "$RESET"
if have moko; then
  note "Next, get set up (timezone, dream hours, your LLM provider):"
  printf '\n      %s%smoko setup%s\n\n' "$BOLD" "$PINK" "$RESET"
else
  note "Almost there — 'moko' isn't on your PATH in this shell yet."
  note "Open a new terminal (or run 'uv tool update-shell'), then run:"
  printf '\n      %s%smoko setup%s\n\n' "$BOLD" "$PINK" "$RESET"
fi
note "Everything runs on your machine. Moko's mind is a single file under ~/.moko/."
