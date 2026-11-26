# The one agent image (ADR-0007): every agent, the shell, base tools and mise toolchains.
# UNVERIFIED: written on a host with no Docker daemon. See docs/spikes/mise-readonly-volume.md.
#
# Layout (the Session runs with --read-only, so only volumes and tmpfs are writable):
#   /opt/node        Private Node that runs the npm-installed agents/tools. Private to the image, so a
#                    project pinning another Node through mise cannot break an agent.
#   /opt/agents      npm prefix for Claude Code, Codex, OMP, the DBX MCP server and bru.
#   /opt/hermes      Hermes, in its own Python venv (independent of the mise Python).
#   /mise            mise data, cache and state. A *volume* at run time (agent-sandbox-mise):
#                    Docker seeds a new empty volume from what is built here (the global Node
#                    LTS, Python and Go); project toolchains (mise.toml / .tool-versions)
#                    install into it on demand.
#   /agents/<name>   one Agent volume per agent, mounted as that agent's HOME (ADR-0006).
#
# Nothing from the host (agent config, credentials, shell settings) is copied in.
# herdr (the Watcher, ADR-0009) is a static binary in /usr/local/bin; its config is /etc/asb/herdr.toml.
# chrome-devtools-mcp (ADR-0008) is an npm tool; Chrome itself is NOT in the image, it runs on the host.
# The shell prompt arrives in a later ticket.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git jq ripgrep xz-utils unzip tar gzip \
      build-essential pkg-config less procps python3 \
 && rm -rf /var/lib/apt/lists/*

# A non-root user for the image default. At run time `--user <uid>:<gid>` overrides the uid, so
# everything an arbitrary uid must write (agent homes, /mise) is world-writable.
RUN useradd --uid 1000 --create-home --home-dir /home/asb --shell /bin/bash asb

# Bootstrap pins: SHA-256 from GitHub's asset digests for the exact upstream release
# tags (api.github.com/repos/<owner>/<repo>/releases/tags/<tag>). Update URL and digest together.
COPY fetch-verified.sh /opt/asb/fetch-verified.sh
# ---- mise (https://github.com/jdx/mise/releases/tag/v2026.11.14).
RUN case "$(dpkg --print-architecture)" in \
      amd64) arch=x64; sha=6fa596291e9758106f761fea4f8e7fac6c3e7ff4714d5c0b9a76e16068db14f6 ;; \
      arm64) arch=arm64; sha=7bd3b2d5ffff077a7662ba186c09600e5b40c1eee9df0c264ed67642ce0a8698 ;; \
      *) echo "unsupported mise architecture" >&2; exit 1 ;; \
    esac \
 && sh /opt/asb/fetch-verified.sh \
      "https://github.com/jdx/mise/releases/download/v2026.11.14/mise-v2026.11.14-linux-$arch" "$sha" /usr/local/bin/mise \
 && chmod 0755 /usr/local/bin/mise \
 && mise --version

# mise keeps data, cache and state in /mise (the shared volume). The global defaults are system
# config rather than anything under a HOME, so every agent and every Workspace sees them.
ENV MISE_DATA_DIR=/mise \
    MISE_CACHE_DIR=/mise/cache \
    MISE_STATE_DIR=/mise/state \
    MISE_GLOBAL_CONFIG_FILE=/etc/mise/config.toml \
    MISE_YES=1
RUN mkdir -p /etc/mise /mise \
 && mise use --global node@lts python@latest go@latest \
 && mise reshim \
 && rm -rf /mise/cache \
 && mkdir -p /mise/cache /mise/state \
 && chmod -R a+rwX /mise \
 && chmod a+r /etc/mise/config.toml

# ---- A private Node for npm agents and Hermes web (upstream requires Node >=26).
# Independent of the default Workspace Node LTS, and not on a mutable volume.
RUN MISE_DATA_DIR=/opt/agent-node MISE_CACHE_DIR=/tmp/mise-cache mise install node@26 \
 && ln -s "$(MISE_DATA_DIR=/opt/agent-node mise where node@26)" /opt/node \
 && rm -rf /tmp/mise-cache

# ---- Claude Code, Codex, OMP (with bun), the DBX MCP server and bru, from npm: latest at build
# time; `asb update` rebuilds without cache (ADR-0012). wrap-node-bins pins their `node`
# shebangs to /opt/node so a project's mise-pinned Node never changes how an agent runs.
# chrome-devtools-mcp is the one pinned exception to ADR-0012 (ADR-0021): asb-chrome-mcp depends on its
# --browser-url flag and connection behaviour, so a bump is a reviewed change, not whatever `latest` is.
ARG CHROME_DEVTOOLS_MCP_VERSION=1.14.0
COPY wrap-node-bins.sh /usr/local/bin/wrap-node-bins
RUN chmod 0755 /usr/local/bin/wrap-node-bins \
 && PUPPETEER_SKIP_DOWNLOAD=1 PATH="/opt/node/bin:$PATH" npm install -g --prefix /opt/agents --no-fund --no-audit \
      @anthropic-ai/claude-code @openai/codex @oh-my-pi/pi-coding-agent bun \
      @dbx-app/mcp-server @usebruno/cli "chrome-devtools-mcp@${CHROME_DEVTOOLS_MCP_VERSION}" \
 && wrap-node-bins /opt/agents/bin /opt/agents/bin-wrapped /opt/node/bin/node \
 && chmod -R a+rX /opt/agents \
 && rm -rf /root/.npm

# ---- asb-chrome-mcp: runs chrome-devtools-mcp against the Host Browser, connecting by IP address to
# host.docker.internal:9222 (Chrome checks the Host header). Reachable only after `asb browser enable`.
COPY asb-chrome-mcp /usr/local/bin/asb-chrome-mcp
RUN chmod 0755 /usr/local/bin/asb-chrome-mcp

# ---- Hermes: its own venv; state goes to $HOME of the Hermes Agent volume, never into the image.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN case "$(dpkg --print-architecture)" in \
      amd64) arch=x86_64; sha=5597970380083a340b7983af05a84824b4db1c62b7059c796935b7fd6dc95fd2 ;; \
      arm64) arch=aarch64; sha=68174084fe40b6300361c5b2038066120dc62c92920c22a0f31f6fc74b066b57 ;; \
      *) echo "unsupported uv architecture" >&2; exit 1 ;; \
    esac \
 && sh /opt/asb/fetch-verified.sh \
      "https://github.com/astral-sh/uv/releases/download/0.13.10/uv-$arch-unknown-linux-musl.tar.gz" "$sha" /tmp/uv.tar.gz \
 && tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 "uv-$arch-unknown-linux-musl/uv" \
 && rm /tmp/uv.tar.gz \
 && uv --version
# v0.23.4, pinned to the upstream release's full commit. Sync its locked Python dependencies.
RUN rev=8fc0374447313e276f6b9a2764bfc750190c966a \
 && git init /opt/hermes/src \
 && cd /opt/hermes/src \
 && git fetch --depth 1 https://github.com/NousResearch/hermes-agent.git "$rev" \
 && git checkout --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$rev" \
 && rm -rf .git \
 && UV_PROJECT_ENVIRONMENT=/opt/hermes/venv uv sync --locked --no-dev --extra web --managed-python --python 3.14 \
 && chmod -R a+rX /opt/hermes /opt/uv-python

# ---- Hermes dashboard (Portal, ADR-0010, ADR-0020). Hermes would build its web UI on first start,
# but the Session's root filesystem is read-only, so build it here and point HERMES_WEB_DIST at it
# (the built-in Portal entry sets that). pnpm is only a build tool: it and the node_modules it
# creates are removed again. The index.html check makes a failed UI build fail the image build.
# UNVERIFIED: no Docker daemon when written; the build steps follow upstream's
# scripts/build/web.mjs. See docs/spikes/hermes-dashboard.md.
RUN cd /opt/hermes/src \
 && pnpm_version=$(/opt/node/bin/node -p 'require("./pm/lock.json").packages.pnpm.version') \
 && PATH="/opt/node/bin:$PATH" npm install -g --prefix /tmp/pnpm-tool --no-fund --no-audit --ignore-scripts "pnpm@$pnpm_version" \
 && PATH="/opt/node/bin:/tmp/pnpm-tool/bin:$PATH" pnpm install --frozen-lockfile --trust-lockfile --prod=false --filter . --filter web... \
 && PATH="/opt/node/bin:/tmp/pnpm-tool/bin:$PATH" node scripts/build/web.mjs \
      --source /opt/hermes/src --icons /opt/hermes/src --out /opt/hermes/src/hermes_cli/web_dist \
 && test -f /opt/hermes/src/hermes_cli/web_dist/index.html \
 && rm -rf /tmp/pnpm-tool /root/.npm /root/.local/share/pnpm /root/.cache /opt/hermes/src/node_modules /opt/hermes/src/web/node_modules \
 && chmod -R a+rX /opt/hermes

# ---- This Workspace's Hermes Kanban home (ADR-0020): an empty world-writable directory that Docker
# copies into a new per-Workspace volume mounted here, so any --user can write the board database.
RUN mkdir -p -m 0777 /kanban

# ---- Plannotator (Portal, ADR-0010): binary only (`--minimal`: no per-agent integrations written
# into any HOME). It lives in the image, not in an Agent volume. Without PLANNOTATOR_REMOTE it
# would try to open a browser; the built-in Portal entry sets PLANNOTATOR_REMOTE=1 and the port,
# so it listens on 0.0.0.0:19432 inside the Session and `asb portal start plannotator` publishes it.
# Claude Code's plan hook is system-wide managed settings (read-only, image layer), so it needs
# nothing from the host or from the Claude volume. UNVERIFIED: see docs/spikes/portals.md.
RUN case "$(dpkg --print-architecture)" in \
      amd64) arch=x64; sha=90c68eedde7458dec1ce98991805cd1bc0e038aa9811fe7e605f31f8048b0a1f ;; \
      arm64) arch=arm64; sha=f1b62fd17181b4b85f3f7544a5d99dbd3dac6ca40e5b09463c7d573165994980 ;; \
      *) echo "unsupported Plannotator architecture" >&2; exit 1 ;; \
    esac \
 && mkdir -p /opt/plannotator/.local/bin \
 && sh /opt/asb/fetch-verified.sh \
      "https://github.com/backnotprop/plannotator/releases/download/v0.29.4/plannotator-linux-$arch" "$sha" \
      /opt/plannotator/.local/bin/plannotator \
 && chmod 0755 /opt/plannotator/.local/bin/plannotator \
 && /opt/plannotator/.local/bin/plannotator --version
COPY claude-managed-settings.json /etc/claude-code/managed-settings.json
RUN chmod 0644 /etc/claude-code/managed-settings.json

# ---- Agent homes: one empty world-writable directory per shipped agent. Docker copies it into a
# new, empty Agent volume, which is what lets an arbitrary --user write to a fresh volume.
# Agents added only through config get no pre-created home; `asb check` warns about that.
RUN for a in hermes omp claude codex; do mkdir -p -m 0777 "/agents/$a"; done

# ---- herdr, the Watcher (ADR-0009): the verified official release binary for this CPU.
# `asb attach` runs it with `docker exec`; Hermes, OMP and shells are its panes. Its
# integrations are installed by `asb up` into the Agent volumes, not here (they live in $HOME).
# HERDR_CONFIG_PATH keeps its config in the read-only image; its state and socket go under HOME (a tmpfs).
RUN case "$(dpkg --print-architecture)" in \
      amd64) arch=x86_64; sha=14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d ;; \
      arm64) arch=aarch64; sha=17d7536f2da50859523680ef279dffcac8e4a235f4e46fe8da6c2ec44141897c ;; \
      *) echo "unsupported herdr architecture" >&2; exit 1 ;; \
    esac \
 && sh /opt/asb/fetch-verified.sh \
      "https://github.com/herdrdev/herdr/releases/download/v0.9.5/herdr-linux-$arch" "$sha" /usr/local/bin/herdr \
 && chmod 0755 /usr/local/bin/herdr \
 && herdr --version
COPY herdr.toml /etc/asb/herdr.toml
ENV HERDR_CONFIG_PATH=/etc/asb/herdr.toml

# ---- Agent wrappers: a pane inherits the Session's HOME (/tmp), so `hermes` typed in a herdr pane
# would lose its login. /opt/asb/bin/<agent> runs the stock agent with its own HOME (ADR-0006).
# /opt/asb/asb-agent <name> uses the Workspace's config, supplied on stdin at Session creation.
COPY asb-agent /opt/asb/asb-agent
RUN chmod 0755 /opt/asb/asb-agent \
 && ln -s /opt/asb/asb-agent /usr/local/bin/asb-agent \
 && mkdir -p /opt/asb/bin \
 && for a in hermes omp claude codex; do ln -s ../asb-agent "/opt/asb/bin/$a"; done

# mise shims first: a project's pinned node/python/go win for everything run in the Workspace.
# The agents themselves are launched from bin-wrapped, which is fixed to the private Node.
ENV PATH=/opt/asb/bin:/mise/shims:/opt/plannotator/.local/bin:/opt/agents/bin-wrapped:/opt/agents/bin:/opt/hermes/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/tmp

USER 1000:1000
WORKDIR /workspace
CMD ["sleep", "infinity"]
