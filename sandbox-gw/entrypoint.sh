#!/bin/sh
# Apply the egress policy into THIS netns, then idle. The `sandbox` container
# joins this netns (network_mode: "service:sandbox-gw") and runs with
# cap_drop: [ALL], so nothing inside it can alter or delete the ruleset.
set -e

nft -f /etc/nftables.conf

# SANDBOX_EGRESS_DENY (comma-separated CIDRs, empty by default) is for the
# HOST's own public IP: Docker DNATs hairpin traffic to published ports, which
# the built-in drop set does not cover. Elements join the sets AFTER the table
# exists. An invalid CIDR fails `nft add element` and, via set -e, the
# container itself — visible, not silent.
if [ -n "${SANDBOX_EGRESS_DENY:-}" ]; then
    DENY=$(printf '%s' "$SANDBOX_EGRESS_DENY" | tr -d ' \t')
    OLDIFS=$IFS
    IFS=,
    for cidr in $DENY; do
        [ -n "$cidr" ] || continue
        case "$cidr" in
            *:*) nft add element inet lore_sandbox egress_deny_v6 "{ $cidr }" ;;
            *)   nft add element inet lore_sandbox egress_deny_v4 "{ $cidr }" ;;
        esac
    done
    IFS=$OLDIFS
fi

exec sleep infinity
