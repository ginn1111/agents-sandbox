# Chrome runs on the host as a Host Browser, opt-in, with a dedicated profile

The agent drives a Chrome running on the host: `chrome-devtools-mcp` is in the image and connects to the host Chrome's remote-debugging port. You can watch the window. Chrome is not installed in the image, which keeps the image smaller and avoids Chrome's sandbox and shared-memory problems in containers. Because remote debugging gives the agent full control of a host browser, Chrome is not an ordinary Host Service but a separate, opt-in Host Browser.

## Rules

- **Off by default.** Enabled only from global config or a CLI flag, never from project config. This is an exception to ADR-0005.
- **`check` warns every Session** when it's enabled: "agent controls host Chrome: intranet and `file:///` reachable".
- **The sandbox never starts Chrome.** It never runs programs on the host. `doctor` prints the exact launch command, and the human runs it.
- **Dedicated profile only.** The launch command uses its own `--user-data-dir`, never the everyday profile, so no cookies, logins or saved passwords are exposed.

## Considered Options

- **Headless Chrome inside the sandbox**: rejected by choice. Fully inside the boundary, but there's no window to watch, the image grows by about 300–400 MB, and it needs `--no-sandbox` fallbacks and a larger `--shm-size`.
- **Treat Chrome as an ordinary Host Service**: rejected. It would contradict the Host Service rule that the agent never reaches host files or other networks through it.
- **Chrome policies blocking `file://` and limiting URLs**: deferred. Possible later hardening; the policies are fiddly and differ by OS.

## Consequences

- **This is a deliberate exception to ADR-0001 and ADR-0002.** When enabled, through Chrome the agent can reach anything the host can: company intranet, LAN services Netguard blocks, and `file:///` paths on the host.
- Unverified, must be tested: Chrome binds remote debugging to host `127.0.0.1`. On Linux, containers can't reach host loopback, so something must listen on an address the Docker bridge can reach. Chrome may also reject DevTools requests whose `Host` header isn't `localhost` or an IP address, so the MCP should connect by IP rather than `host.docker.internal`.
