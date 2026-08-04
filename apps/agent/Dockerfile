# syntax=docker/dockerfile:1
#
# Moko in a box (SANDBOX.md). Built from source — the machine runs what the repo
# says, no prebuilt registry image to trust.
#
# You don't normally build this by hand: with runtime.mode = podman|docker,
# `moko service install` builds it automatically (from a local checkout, or straight
# from git when Moko is installed as a tool) and launches. To build manually:
#
#   podman build -t moko:local apps/agent      # named Dockerfile → auto-found, no -f
#   docker build -t moko:local apps/agent
#
# Reproducibility boundary: pin BASE by digest for a real release; together with
# the committed uv.lock that is the actual supply-chain surface (not this file).

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ca-certificates for TLS to Telegram/LLM APIs; curl for the Claude Code installer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# An unprivileged user. Its home holds the Claude Code login, bind-mounted at run
# time so it survives image rebuilds (see ContainerRuntime / `moko login`).
RUN useradd --create-home --home-dir /home/moko moko
ENV HOME=/home/moko \
    PATH=/home/moko/.local/bin:/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    UV_LINK_MODE=copy

# Install Moko from source against the committed lockfile (--frozen = honour uv.lock).
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev \
 && ln -sf /app/.venv/bin/moko /usr/local/bin/moko \
 && chown -R moko:moko /app

USER moko

# Claude Code — the standalone native binary (no Node). The login itself is done
# later, inside the running container, via `moko login`, writing into the
# bind-mounted ~/.claude. A failed fetch is non-fatal: the container can instead use
# an API key / xAI OAuth from ~/.moko/secrets.yaml.
RUN curl -fsSL https://claude.ai/install.sh | bash \
 || echo "claude install skipped — verify the installer/network at build time"

# ~/.moko (mind, config, secrets, control socket) is bind-mounted at run time at the
# identical host path, with MOKO_HOME set to match. The monitor binds 0.0.0.0 inside
# the container and is published to host loopback only (MOKO_MONITOR_HOST=0.0.0.0).
CMD ["moko", "start"]
