# The Host Browser opens through the Netguard helper on port 9222, and the agent connects by IP address

ADR-0008 makes Chrome an opt-in Host Browser and ADR-0013 says it is opened only by an explicit command. We decided: global config (`network.host_browser: true`) or the `--host-browser` flag only makes it *available*; `check`, `up` and `doctor` say so with the warning "agent controls host Chrome: intranet and `file:///` reachable". `asb browser enable` then runs `docker exec <netguard> asb-service enable host-browser host.docker.internal 9222`, the same helper and `NG_SERVICES` chain as Host Services (ADR-0016), so one TCP port on the host gateway opens and nothing else does. `asb browser disable` removes it; `down` removes it with Netguard. The name `host-browser` and the address `host.docker.internal:9222` are reserved: a configured Host Service cannot use either, so a project cannot open Chrome's port through `asb service enable`, skipping the opt-in. The agent image ships `chrome-devtools-mcp` and `asb-chrome-mcp`, a wrapper with a fixed target that resolves `host.docker.internal` itself and connects to `http://<ip>:9222`, by IP address because Chrome rejects DevTools requests whose `Host` header is not `localhost` or an IP. `asb doctor` prints the Chrome launch command (`--remote-debugging-port=9222`, `--user-data-dir` under the state directory, never in a Workspace) and, on Linux, a `socat` relay command bound to the Docker gateway address only. The CLI runs neither; the human does. A profile directory inside the Workspace makes the plan Blocked, because the agent could read the cookies.

## Considered Options

- **A second Netguard rule path just for Chrome**: rejected. The helper already validates the address, resolves it, verifies and rolls back; a second path would repeat that with its own bugs.
- **Let the CLI start Chrome or the relay**: rejected. ADR-0008: the CLI never runs host programs.
- **`--remote-debugging-address=0.0.0.0` on Chrome so no relay is needed**: rejected as the documented path. It puts a full browser-control port on every interface of the host, LAN included. Recent Chrome also ignores the flag in some versions; the relay on the gateway address keeps the exposure to the Docker bridge.
- **Connect with `host.docker.internal` in the URL**: rejected; Chrome's Host header check would refuse it (UNVERIFIED, docs/spikes/host-browser.md).
- **Allow `host_browser` in project config when set to `false`**: rejected. The key stays invalid there, so the rule is simple and the message is the same whichever value is written.

## Consequences

- On Linux a loopback-only Chrome is unreachable until the human runs the relay; `doctor` probes from the Session after `enable` and reports exactly that case. On Colima and Docker Desktop whether `host.docker.internal:9222` reaches the host's loopback depends on the VM's forwarding (UNVERIFIED).
- While enabled, the agent controls a full browser on the host: the intranet, LAN services Netguard blocks, and `file:///` paths on the host are reachable through it. The dedicated profile limits what cookies and logins exist; it does not limit where the browser can go. Chrome URL policies stay deferred (ADR-0008).
- Port 9222 is fixed. A different port means a different launch command and a code change; one fewer input to validate.
- `asb-chrome-mcp` accepts extra arguments for `chrome-devtools-mcp`, but it is not a security boundary: Netguard is. Only the one port opened by `enable` is reachable.
