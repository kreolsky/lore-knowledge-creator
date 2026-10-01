#!/bin/bash
# Hand off to sshd. The egress ruleset is NOT applied here: sandbox-gw
# owns this netns and applies it (sandbox-gw/nftables.conf) with its own
# NET_ADMIN — this container runs with cap_drop: [ALL] plus sshd's minimal cap
# set, so neither root nor the agent in here can alter or delete it.
#
# INVARIANT(security): the sandbox reaches the internet but NEVER the app network.
# Why: the Tool-API is the agent's permission boundary — a direct socket to surreal,
# redis or the backend's own HTTP surface would route around every access check.
#
# Being on sandbox-net is NOT sufficient on its own: the backend must join that
# network to reach sshd here, and a docker network is bidirectional — so without
# the gw's ruleset the sandbox can dial backend:8001 straight back. Verified the
# hard way: before the ruleset existed, `socket.create_connection(("backend", 8001))`
# from inside sandbox_bash SUCCEEDED while surreal/redis were already blocked.
#
# Bundled-key installs (compose with the sandbox-keygen volume): the generated
# public half replaces the baked file when the mount provides it. Without the
# mount the baked key stays — dev with .env SANDBOX_SSH_KEY_B64 is unchanged.
if [ -f /sandbox-keys/pub/authorized_keys ]; then
    install -o agent -g agent -m 600 /sandbox-keys/pub/authorized_keys /home/agent/.ssh/authorized_keys
fi

exec /usr/sbin/sshd -D -e
