# Scope is workspace confinement and secret masking, not containing a hijacked agent

The sandbox exists to do two things: limit an agent to one Workspace directory, and Mask Secret files inside it. It is a lightweight daily tool, not a staged MVP that grows into a hardened product. It does not try to stop a hijacked or malicious agent from tampering with the Workspace (e.g. editing `Makefile`, `package.json` or `.git/hooks` so code later runs on the host). Doing that would need sanitized copy mode, diff review and an enforced network allowlist, which would defeat "lightweight". These limits must be stated plainly in the README and in `check` output, and the tool must never claim more.

## Consequences

- The PROPOSE.md split between "MVP" and "hardened release" (M4: allowlist, sanitized copy) is dropped. Whatever the tool claims must be true when it ships.
- Features in PROPOSE.md that serve neither goal (confinement, masking) must justify their weight or be cut.
