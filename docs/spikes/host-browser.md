# Spike: Host Browser through `asb browser enable` (ticket #11, ADR-0008, ADR-0013, ADR-0020)

**Status: UNVERIFIED.** Written on a host with no Docker daemon and no Chrome run, so nothing below was run. Each item is an assumption, how the design fails closed if it is wrong, and the check that settles it. Run `uv run python -m pytest -m docker tests/test_e2e_host_browser.py` on Linux Docker and on Colima (Docker Desktop if available), then replace each **Result:** line. The ticket's spike question (how the debugging port is reachable from the Session on Linux and Colima: loopback bind and Host-header check) is answered below as a design, to be confirmed by items 1–3.

## 1. Chrome binds the debugging port to 127.0.0.1, and a Linux container cannot reach it

- Assumption: `google-chrome --remote-debugging-port=9222 --user-data-dir=<dedicated>` listens on `127.0.0.1:9222` (IPv4 loopback; possibly `[::1]` too). `--remote-debugging-address` is ignored by current headed Chrome. On Linux the Session reaches the host through the Docker bridge gateway (`host.docker.internal` → `host-gateway`, usually `172.17.0.1`), which is not loopback, so the connection is refused even after Netguard opens the path.
- Design: `asb doctor` prints `socat TCP-LISTEN:9222,bind=<bridge gateway>,fork,reuseaddr TCP:127.0.0.1:9222`, bound to the gateway address only, never `0.0.0.0`. The human runs it; the CLI never does. The gateway comes from `docker network inspect bridge`; an address `relay_command` does not accept (loopback, `0.0.0.0`, link-local, junk) yields a placeholder instead of a command.
- Fails closed: with no relay nothing answers; `doctor` (after `enable`) reports "does not answer from the Session". Nothing is widened.
- Check: `test_an_agent_can_open_a_page_in_host_chrome_through_the_mcp` (starts Chrome and `socat` itself, as the human would; skips without them).
- Source reading (not a run): `chrome/browser/devtools/remote_debugging_server.cc` (Chromium main) listens with `ListenWithAddressAndPort("127.0.0.1", port)` and falls back to `"::1"`; it never binds a non-loopback address unless told to. The same file refuses to start the server on the default user-data-dir (`NotStartedReason::kDisabledByDefaultUserDataDir`, Chrome 136+), so the dedicated `--user-data-dir` in the launch command is required for the port to open at all, not only for privacy.
- Result: _not run_.

## 2. DevTools requests must carry an IP-address Host header

- Assumption: Chrome answers `/json/version` and the WebSocket upgrade only when the `Host` header is `localhost` or an IP address, and answers 500 ("Host header is specified and is not an IP address or localhost") for a name such as `host.docker.internal`. `asb-chrome-mcp` therefore resolves the name itself and connects to `http://<ip>:9222` (ADR-0020).
- Also unverified: `webSocketDebuggerUrl` in `/json/version` carries `ws://127.0.0.1:9222/...` or the request's Host. `chrome-devtools-mcp --browser-url` fetches `/json/version` and then connects to the URL Chrome returns. If Chrome returns `127.0.0.1`, the MCP would connect to the Session's own loopback and fail. If that happens, switch the wrapper to `--ws-endpoint=ws://<ip>:9222/devtools/browser/<id>` (read from `/json/version` and with the host part replaced), still by IP; do not loosen Netguard.
- Fails closed: a refused or malformed answer means the MCP cannot connect; no other path opens.
- Source reading (not a run), `content/browser/devtools/devtools_http_handler.cc` (Chromium main):
  - `RequestIsSafeToServe` serves an HTTP or WebSocket request only if the `Host` header is empty, an IP address, or a local host name (`localhost`). `host.docker.internal` is refused; `172.17.0.1:9222` is accepted. This confirms the by-IP design.
  - `/json/version` builds `webSocketDebuggerUrl` as `ws://<request Host header><browser path>`. Because the wrapper connects to `http://<ip>:9222`, the URL Chrome returns is `ws://<ip>:9222/devtools/browser/<id>`, reachable through the same Netguard rule. The `--ws-endpoint` fallback above should not be needed.
  - The WebSocket upgrade is refused if it carries an `Origin` header that is neither the server's own IP nor listed in `--remote-allow-origins`. Puppeteer (Node `ws`), used by `chrome-devtools-mcp`, sends no `Origin`, so no `--remote-allow-origins` flag is needed. Do **not** add `--remote-allow-origins=*` to the launch command: that would let any web page the human visits drive the debugging port.
- `chrome-devtools-mcp` 1.14.0 (the pinned version) connects with `puppeteer.connect({browserURL})` when given `--browser-url` (`BrowserManager.js#connect`), i.e. it fetches `/json/version` and follows `webSocketDebuggerUrl`. Read from the published package, not run.
- Check: `test_the_wrapper_probe_gets_chrome_s_version_by_ip` (stand-in that applies Chrome's Host rule), `test_an_agent_can_open_a_page_in_host_chrome_through_the_mcp` (real Chrome).
- Result: _not run_.

## 3. Colima and Docker Desktop

- Assumption: `host.docker.internal` resolves to the VM's host address (Colima/Lima: `192.168.5.2`; Docker Desktop: its own). Traffic to it reaches macOS loopback services in Lima (`192.168.5.2` is forwarded to the host), so Chrome on `127.0.0.1:9222` needs no relay. Not guaranteed: on Docker Desktop for Mac, `host.docker.internal` reaches loopback-bound host apps; for Colima it depends on the VM network mode.
- Fails closed: if it does not answer, `doctor` says so; `enable` itself succeeded in opening only the one port.
- `doctor` shows the relay only when `sys.platform` starts with `linux`; on macOS it shows none. Run Check 1 on Colima to see whether that is right.
- Check: the same e2e test on Colima.
- Result: _not run_.

## 4. The `NG_SERVICES` rule opens 9222 and nothing else on the host

- Assumption: as in docs/spikes/host-services.md items 1–3: the helper's ACCEPT precedes the range drops; any other host port stays dropped; the `host-browser` rule set is removed by `disable` and by `down`.
- Check: `test_enabling_the_host_browser_opens_9222_and_no_other_host_port`, `test_disable_closes_the_host_browser_again`.
- Result: _not run_.

## 5. A Host Service cannot take the Host Browser's place

- Assumption (code, not Docker): config validation refuses the name `host-browser` and the address `host.docker.internal:9222` for a Host Service. A different host name for the same machine (the host's LAN IP on 9222) is not caught: it is a Host Service to a port the human chose to open, and Chrome only listens on it if the human started the relay on that address.
- Check: Seam 1 `test_a_host_service_may_not_take_the_name_or_address_the_host_browser_uses` (tests/test_host_browser.py).
- Result: verified by Seam 1 (no Docker needed).

## 6. `file://` and intranet reach through Chrome (the accepted exposure)

- Assumption: with the Host Browser open, the agent can `navigate_page` to `file:///` paths and intranet URLs. This is the documented exception to ADR-0001 and ADR-0002. Nothing in the design prevents it; it is shown by every `check`, `up`, `enable` and `doctor`, and listed in the Limits.
- Check: none; this is a documented limit, not a control.
- Result: _not run_.

## 7. `chrome-devtools-mcp` on the private Node

- Assumption: `npm install -g --prefix /opt/agents chrome-devtools-mcp` works with Node LTS and `wrap-node-bins` pins its shebang to `/opt/node`, so it runs when a project pins another Node through mise. It needs no Chrome binary when connecting with `--browser-url` (it uses `puppeteer-core`). If the package downloads a browser in a postinstall step, the image would grow: check `docker image history` and set `PUPPETEER_SKIP_DOWNLOAD=1` if so.
- Check: `test_the_image_has_chrome_devtools_mcp_and_no_chrome`.
- Result: _not run_.

## Manual check (exact steps)

Run these once on each platform, then fill in the **Result:** lines above. The CLI never runs Chrome or `socat`; you do.

### Linux Docker

1. `uv run asb build`, then write `network: {host_browser: true}` (with `version: 1`) to `~/.config/agent-sandbox/config.yaml`, or pass `--host-browser` to every command below.
2. `uv run asb check <ws>` and `uv run asb up <ws>`: both print the warning "agent controls host Chrome: intranet and file:/// reachable".
3. `uv run asb doctor <ws>`: copy the Chrome command and the `socat` relay command it prints. Run Chrome; check `ss -ltnp | grep 9222` shows `127.0.0.1:9222` only (item 1). Run the relay; `ss -ltnp | grep 9222` now also shows `<gateway>:9222` (e.g. `172.17.0.1`), never `0.0.0.0`.
4. Before enabling: `docker exec asb-<…> asb-chrome-mcp --probe` fails (Netguard drops it).
5. `uv run asb browser enable <ws>`; then the probe prints Chrome's `/json/version` with `webSocketDebuggerUrl` starting `ws://<gateway>:9222/` (item 2).
6. `docker exec asb-<…> curl -s -H 'Host: host.docker.internal' http://<gateway>:9222/json/version` answers 500 "Host header is specified and is not an IP address or localhost" (item 2).
7. `docker exec asb-<…> bash -c 'exec 3<>/dev/tcp/<gateway>/22'` (or any other listening host port) still fails (item 4).
8. In the Session: `claude mcp add chrome-devtools -- asb-chrome-mcp`, ask the agent to open `https://example.com`; the tab appears in the host Chrome window.
9. `uv run asb browser disable <ws>`; the probe fails again. `uv run asb down <ws>`.
10. Or run all of it at once: `uv run python -m pytest -m docker tests/test_e2e_host_browser.py` (the real-Chrome test needs `google-chrome` and `socat` on the host).

### Colima (macOS)

Same steps, except step 3: `doctor` prints no relay on macOS. Start Chrome with the printed command (it binds `127.0.0.1:9222` on macOS). After `asb browser enable`, run the probe (step 5):

- If it answers, Lima forwards `host.docker.internal` (`192.168.5.2` by default) to macOS loopback and no relay is needed: record it in item 3.
- If it does not, record the Colima network mode (`colima status`, `--network-address` or not) and whether a relay bound to the address `host.docker.internal` resolves to on macOS fixes it; then make `doctor` print that relay for macOS too (code change, ADR-0020 consequence). Do not bind Chrome or the relay to `0.0.0.0`.
