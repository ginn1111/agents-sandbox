# Portal publisher (ADR-0018): one tiny container per started Portal. It publishes
# 127.0.0.1:<random>:8000 on the host and forwards to the Portal port on Netguard's address on a
# private per-Portal network. No capabilities, no mounts, no env, runs as nobody.
FROM alpine:3.20
RUN apk add --no-cache socat
USER 65534:65534
ENTRYPOINT ["socat"]
