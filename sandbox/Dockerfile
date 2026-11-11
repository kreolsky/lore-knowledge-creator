# The agent's console host, dev half (prod is CT 703 on black).
# See SYSTEM: agent-sandbox (backend/routes/tool_api/sandbox/).
#
# ARCH: this image is NOT the sandbox contract — SSH is. The backend knows only
# SANDBOX_SSH_HOST + a private key + a path; it never knows whether the far end is
# this container (dev) or the Proxmox LXC CT 703 (prod). The agent's ENVIRONMENT
# (apt toolchain, Node, the Python stack, Playwright) is provisioned from ONE
# source shared by both: sandbox/provision.sh, run here at image build and by
# the prod deploy on CT 703 (.gitea/workflows/deploy.yml). The host's plumbing
# stays per host and in this file: an `agent` unix user, key-only sshd,
# /workspace.
#
# INVARIANT(security): only the PUBLIC key is baked here. Why: the image is
# rebuildable by anyone with the repo, so the private half would be a published
# credential; it lives only in the backend's env (SANDBOX_SSH_KEY_B64).
FROM python:3.12-slim

# The image's plumbing only (sshd); the environment — apt toolchain, Node, the
# Python stack, Playwright — is installed by provision.sh, ONE source shared
# with CT 703. Kept in the image at /lore-provision so a re-run in place
# (idempotent) and the manifest's requirements.txt stay the repo's copy.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/*

COPY provision.sh requirements.txt /lore-provision/
# bash-invoked: COPY does not carry the exec bit, and the prod deploy runs the
# same script as `bash provision.sh` on CT 703.
RUN bash /lore-provision/provision.sh

# INVARIANT(security): the agent user is NEVER root and NEVER gets sudo. Why:
# this box exists to run arbitrary code, and the egress boundary is enforced
# OUTSIDE it (sandbox-gw's ruleset in the shared netns, which no caps in here
# can reach); on prod CT 703 the same boundary is the root-owned nftables
# ruleset — a sudo-capable agent could flush it and reach the home LAN. Keep
# the two ends identical so dev never teaches a habit prod forbids.
RUN useradd --create-home --shell /bin/bash --uid 1000 agent \
    && mkdir -p /workspace /var/run/sshd \
    && chown agent:agent /workspace

COPY authorized_keys /home/agent/.ssh/authorized_keys
RUN chown -R agent:agent /home/agent/.ssh \
    && chmod 700 /home/agent/.ssh \
    && chmod 600 /home/agent/.ssh/authorized_keys

# Key-only, no root login, no passwords.
RUN { \
      echo 'PermitRootLogin no'; \
      echo 'PasswordAuthentication no'; \
      echo 'KbdInteractiveAuthentication no'; \
      echo 'PubkeyAuthentication yes'; \
      echo 'AllowUsers agent'; \
      echo 'AcceptEnv LANG LC_* LORE_*'; \
    } > /etc/ssh/sshd_config.d/lore-sandbox.conf

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
