# The sandbox CLI is written in Python and drives the Docker CLI

`agent-sandbox` is a Python package (Python 3.11+), installed with `uv tool install` or `pipx`. It drives containers by running the `docker` CLI as a subprocess rather than through the Docker SDK, so it uses whatever Docker context the user already has (Linux Docker, Colima, Docker Desktop) with no socket configuration. Dependencies stay minimal: standard library plus a YAML parser.

## Considered Options

- **Go** (as PROPOSE.md had): rejected by choice. A single static binary is easier to distribute, but Python is faster to write and change for the team, and the CLI is glue around `docker` with no performance-sensitive work.
- **Docker SDK for Python**: rejected. Another dependency, and it needs its own handling of Docker contexts and Colima socket paths, which the `docker` CLI already resolves.

## Consequences

- Users need Python 3.11+ and `uv` or `pipx` on the host. That's a prerequisite `doctor` checks.
- The Netguard container doesn't depend on the CLI's language: it's a small Alpine image with `iptables` and a shell script, configured by the CLI through arguments.
- Masking, discovery (globs, symlinks, hard links) and config precedence are pure Python and unit-testable without Docker. Mount, network and Portal behaviour need integration tests against a real Docker.
