# The sandbox's network-namespace owner: applies the egress ruleset into the
# netns, then idles. See SYSTEM: agent-sandbox (backend/routes/tool_api/sandbox/).
#
# ARCH: this image holds NO agent code — nftables and a shell, nothing else. It
# owns the netns the `sandbox` container joins (network_mode: "service:sandbox-gw"),
# so the egress ruleset is applied with a NET_ADMIN this image holds and the
# agent-bearing container does not: even root inside the sandbox cannot alter or
# delete it. A filter applied from inside the sandbox would need NET_ADMIN on the
# one container that runs agent-authored code and hold on "agent is not root" alone.
FROM alpine:3.22

RUN apk add --no-cache nftables

COPY nftables.conf /etc/nftables.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh && chmod 644 /etc/nftables.conf

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
