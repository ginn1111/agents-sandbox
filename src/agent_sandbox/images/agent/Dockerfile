# Minimal agent image for the walking skeleton. Agents, herdr, mise and the rest
# arrive in later tickets (ADR-0007); this only has to run a hardened Session.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
CMD ["sleep", "infinity"]
