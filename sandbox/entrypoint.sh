#!/bin/bash
# Apply the egress policy, then hand off to sshd.
#
# INVARIANT(security): the sandbox reaches the internet but NEVER the app network.
# Why: the Tool-API is the agent's permission boundary — a direct socket to surreal,
# redis or the backend's own HTTP surface would route around every access check.
#
# Being on sandbox-net is NOT sufficient on its own: the backend must join that
# network to reach sshd here, and a docker network is bidirectional — so without this
# ruleset the sandbox can dial backend:8001 straight back. Verified the hard way:
# before this file existed, `socket.create_connection(("backend", 8001))` from inside
# sandbox_bash SUCCEEDED while surreal/redis were already blocked.
#
# This deliberately mirrors CT 703's /etc/nftables.conf rule-for-rule (see
# docs/sandbox-access.md): same mechanism (in-guest nftables), same RFC1918 drop. Dev
# and prod must not diverge on the boundary — a dev box that is MORE permissive than
# prod teaches habits prod rejects, and hides exactly this class of hole.
set -e

nft -f /etc/nftables.conf

exec /usr/sbin/sshd -D -e
