#!/bin/sh
# Build the monitor dashboard UI and vendor it into the agent package — for LOCAL
# DEV (installing from a clone / git source).
#
# The built UI (apps/agent/src/moko/monitor/static/) is a build ARTIFACT: it is
# gitignored and NEVER committed. Release wheels get it from CI — .github/workflows/
# release.yml runs these same steps before `uv build`, so a released `moko` serves
# the dashboard with no Node. A plain source build ships without it; run this from a
# clone so `moko` serves the dashboard while you develop. The server picks up the
# folder automatically (see monitor/server.py::_spa_dir).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STATIC="$ROOT/apps/agent/src/moko/monitor/static"

command -v pnpm >/dev/null 2>&1 || {
  echo "pnpm is required to build the monitor UI (https://pnpm.io)." >&2
  exit 1
}

echo "Building the monitor UI…"
pnpm -C "$ROOT" install
pnpm -C "$ROOT" --filter @moko/monitor build

echo "Vendoring into the agent package…"
rm -rf "$STATIC"
cp -r "$ROOT/apps/monitor/dist" "$STATIC"

printf '\nDone → %s\n' "$STATIC"
echo "Commit that folder so the packaged dashboard ships with 'moko'."
