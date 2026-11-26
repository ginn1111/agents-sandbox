# Spike: Hermes dashboard Portal and per-Workspace Kanban (ticket 09, ADR-0010, ADR-0020)

**Status: UNVERIFIED by running.** Written on a host with no Docker daemon, so nothing below was run against a real Hermes in a real container. Items marked *source-read* were checked against upstream's `main` (NousResearch/hermes-agent, Nov 2026) as plain text only; Hermes changes quickly, so the image's own clone is what counts. Each item says how the design fails closed if the assumption is wrong and which test settles it. Run `uv run python -m pytest -m docker tests/test_e2e_hermes_dashboard.py` on Linux Docker and on Colima, then replace each **Result:** line.

## 1. The Host-header check accepts `127.0.0.1:<random>` (acceptance criterion 1, first half)

- Source-read: `host_header_middleware` calls `_is_accepted_host(host_header, bound_host, ...)` (`hermes_cli/web_server_boundary.py`). When `bound_host` is `0.0.0.0` or `::` it returns `True` for any non-empty Host ("explicit opt-in to non-loopback, no protection possible at this layer"). The dashboard is started with `--host 0.0.0.0`, so `Host: 127.0.0.1:49153` should pass. The DNS-rebinding guard is therefore not engaged for this Portal; the login is what protects it, and the publisher binds only `127.0.0.1` on the host (ADR-0018).
- Fails closed: if Hermes does reject the Host, the page answers HTTP 400 "Invalid Host header" and the Portal is unusable. Nothing is weakened; the fix would be `dashboard.public_url`/trusted host config, recorded in a new ADR.
- Check: `test_the_dashboard_accepts_the_random_loopback_host_header`.
- Result: _not run_.

## 2. `HERMES_KANBAN_BOARD` does not stop a dispatcher touching other boards (acceptance criterion 1, second half)

- Source-read: the dispatcher (`gateway/kanban_watchers_dispatcher.py`, `tick_once`) runs `dispatch_once` for **every** slug from `list_boards()` in the Kanban home, every tick. `HERMES_KANBAN_BOARD`, `HERMES_KANBAN_DB` and `HERMES_KANBAN_WORKSPACES_ROOT` are set in the **worker's** environment so a worker sees one board, and `get_current_board()` uses the variable for CLI resolution. Upstream's docs say "One dispatcher sweeps all boards per tick; workers are spawned with `HERMES_KANBAN_BOARD` pinned so they can't see other boards." That is the opposite of "pinning stops the dispatcher touching other boards".
- Decision: the ticket says "if not, keep `kanban.db` per Workspace". ADR-0020 does that: a Kanban home volume per Workspace (`HERMES_KANBAN_HOME=/kanban`), so there is only one Workspace's boards for a dispatcher to sweep. `HERMES_KANBAN_BOARD` is kept as well, for the Kanban tab and CLI.
- Fails closed: if Hermes ignored `HERMES_KANBAN_HOME`, boards would land in `$HOME/.hermes` of the shared Hermes Agent volume and isolation would be lost. `test_the_session_kanban_state_is_in_the_workspaces_own_volume` checks where the database file really is, and `test_a_board_of_another_workspace_is_not_visible` that the other Workspace's board cannot be listed.
- Result: _not run_.

## 3. `hermes kanban boards create <slug>` in the Session

- Assumption: it works as the Session's user with `HOME=/agents/hermes` (the `/opt/asb/bin/hermes` wrapper sets it) and writes `/kanban/kanban/boards/<slug>/board.json` and a database; running it twice is a no-op (upstream `create_board`: "`mkdir -p` semantics: existing board returns its metadata"). Hermes ignores a pinned board that does not exist (`get_current_board` -> `board_exists`), which is why `up` creates it.
- Fails closed: if it fails, `up` warns and keeps the Session. Isolation is unaffected (item 2); the Kanban tab then shows the volume's `default` board.
- Check: `test_up_creates_the_workspace_board_and_it_persists_across_down_and_up`.
- Result: _not run_.

## 4. A new empty volume is writable by any `--user`

- Assumption: the image has `/kanban` mode 0777, Docker copies it into the new empty volume at first mount (the same mechanism as the Agent homes and `/mise`), so uid 1000 or any other can write the database. Tested only by writing a file as the Session user.
- Fails closed: if not writable, board creation fails (item 3) and Hermes cannot keep tasks; nothing is exposed.
- Check: `test_the_session_kanban_state_is_in_the_workspaces_own_volume`.
- Result: _not run_.

## 5. The dashboard starts with a read-only root and a prebuilt UI

- Assumption: `uv pip install -e "/opt/hermes/src[web]"` provides fastapi/uvicorn; `scripts/build/web.mjs --source --icons --out` after `pnpm install --frozen-lockfile --filter web...` produces `hermes_cli/web_dist/index.html`; with `HERMES_WEB_DIST` set, `hermes dashboard` validates that directory and does **not** try to build (`_resolve_dashboard_web_dist`). Upstream's `web/package.json` forbids `npm` and wants `pnpm` (installed here with npm into a throwaway prefix). The build steps follow upstream's `hermes_cli/source_build.py`; they may need a different pnpm or Node version than the private Node 24 in `/opt/node`.
- Fails closed: the image build fails at the `test -f .../index.html` step, so a broken UI build is noticed at `asb build`, never at `portal start`. If the dashboard still needed to write (config, `.env`), it writes under `HOME=/agents/hermes`, which is a volume.
- Check: `test_the_dashboard_serves_its_ui_from_the_image`.
- Result: _not run_.

## 6. The login works in a browser (acceptance criterion 4)

- Source-read: with `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` and `_PASSWORD` set (and `_SECRET` for restart-stable sessions), the bundled `basic` provider activates, and a non-loopback bind requires it. Environment variables win over `config.yaml`. Upstream issues #55130 and #57294 report HTTP 500 on `/` and `/auth/login` when `basic` is the only provider in some versions; if the image's clone has that bug, the dashboard is unreachable until upstream fixes it.
- Fails closed: unauthenticated requests never get the dashboard; a 500 means no access, not open access.
- Check: `test_the_dashboard_requires_the_generated_login`. By hand: `asb portal start hermes-dashboard`, open the URL, sign in with the printed login, open the Kanban tab and see the Workspace's board (named after the directory). This is the part automation cannot prove.
- Result: _not run_.

## 6a. Login cookie and WebSocket through `127.0.0.1:<random>`

- Source-read (upstream `main` 04be1bc): the password-login POST is accepted when the browser's `Sec-Fetch-Site` is `same-origin` (`dashboard_auth/request_utils.py`, `cookie_origin_is_allowed`), which it is for the dashboard's own page at `127.0.0.1:<random>`; Host passes because the bind is `0.0.0.0`. Cookies are bare-named, `HttpOnly`, `SameSite=Lax`, not `Secure` over plain HTTP. The WebSocket gate (`ws_host_origin_reason`) accepts an Origin whose authority equals the Host the browser dialled ("a port forward ... `docker -p 8080:9119`: same-origin by definition"), so the publisher's random port does not break chat or the PTY.
- Fails closed: a mismatch gives 403 or a refused WebSocket, never access without the login.
- Check: by hand, item 6; then use the chat tab, which needs the WebSocket.
- Result: _not run_.

## 7. The dashboard sees the same data as the Hermes pane

- Assumption: the Portal `command` is `hermes ...` and `docker exec` resolves it through the Session's PATH, where `/opt/asb/bin/hermes` (the Agent-volume wrapper) is first, so the dashboard runs with `HOME=/agents/hermes` and shows the same Hermes sessions and config as the pane. If PATH resolution differs, the dashboard would run with `HOME=/tmp` and show an empty Hermes.
- Check: `test_the_dashboard_runs_with_the_hermes_agent_volume_as_home`.
- Result: _not run_.

## 8. Workers cannot reach another Workspace's tasks (acceptance criterion 5)

- Follows from items 2 and 3 only if Hermes honours `HERMES_KANBAN_HOME`; the dispatcher (embedded in `hermes gateway` if the user starts it) runs inside this Session and sees only `/kanban`. Not proven without running two Workspaces with a worker.
- Check: `test_a_board_of_another_workspace_is_not_visible` (two real Sessions, a task in each, each lists only its own).
- Result: _not run_.
