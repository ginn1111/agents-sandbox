# Netguard (ADR-0003): Alpine, iptables and one shell script. Runs as root with only NET_ADMIN
# (the CLI drops every other capability); the Session container is the non-root one.
FROM alpine:3.20
RUN apk add --no-cache iptables iproute2
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0555 /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
