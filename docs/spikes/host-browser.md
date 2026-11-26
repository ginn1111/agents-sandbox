# Spike: Host Browser through `asb browser enable` (ticket #11, ADR-0008, ADR-0013, ADR-0021)

**Status: UNVERIFIED.** Written on a host with no Docker daemon and no Chrome run, so nothing below was run. Each item is an assumption, how the design fails closed if it is wrong, and the check that settles it. Run `uv run python -m pytest -m docker tests/test_e2e_host_browser.py` on Linux Docker and on Colima (Docker Desktop if available), then replace each **Result:** line. The ticket's spike question (how the debugging port is reachable from the Session on Linux and Colima: loopback bind and Host-header check) is answered below as a design, to be confirmed by items 1–3.

## 1. Chrome binds the debugging port to 127.0.0.1, and a Linux container cannot reach it

- Assumption: `google-chrome --remote-debugging-port=9222 --user-data-dir=<dedicated>` listens on `127.0.0.1:9222` (IPv4 loopback; possibly `[::1]` too). `--remote-debugging-address` is ignored by current headed Chrome. On Linux the Session reaches the host through the Docker bridge gateway (`host.docker.internal` → `host-gateway`, usually `172.17.0.1`), which is not loopback, so the connection is refused even after Netguard opens the path.
- Design: `asb doctor` prints `socat TCP-LISTEN:9222,bind=<bridge gateway>,fork,reuseaddr TCP:127.0.0.1:9222`, bound to the gateway address only, never `0.0.0.0`. The human runs it; the CLI never does. The gateway comes from `docker network inspect bridge`; an address `relay_command` does not accept (loopback, `0.0.0.0`, link-local, junk) yields a placeholder instead of a command.
- Fails closed: with no relay nothing answers; `doctor` (after `enable`) reports "does not answer from the Session". Nothing is widened.
- Check: `test_an_agent_can_open_a_page_in_host_chrome_through_the_mcp` (starts Chrome and `socat` itself, as the human would; skips without them).
- Result: _not run_.

## 2. DevTools requests must carry an IP-address Host header

- Assumption: Chrome answers `/json/version` and the WebSocket upgrade only when the `Host` header is `localhost` or an IP address, and answers 500 ("Host header is specified and is not an IP address or localhost") for a name such as `host.docker.internal`. `asb-chrome-mcp` therefore resolves the name itself and connects to `http://<ip>:9222` (ADR-0021).
- Also unverified: `webSocketDebuggerUrl` in `/json/version` carries `ws://127.0.0.1:9222/...` or the request's Host. `chrome-devtools-mcp --browser-url` fetches `/json/version` and then connects to the URL Chrome returns. If Chrome returns `127.0.0.1`, the MCP would connect to the Session's own loopback and fail. If that happens, switch the wrapper to `--ws-endpoint=ws://<ip>:9222/devtools/browser/<id>` (read from `/json/version` and with the host part replaced), still by IP; do not loosen Netguard.
- Fails closed: a refused or malformed answer means the MCP cannot connect; no other path opens.
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
