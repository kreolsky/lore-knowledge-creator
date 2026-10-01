# The agent's console host, dev half (prod is CT 703 on black).
# See SYSTEM: agent-sandbox (backend/routes/tool_api/sandbox/).
#
# ARCH: this image is NOT the sandbox contract — SSH is. The backend knows only
# SANDBOX_SSH_HOST + a private key + a path; it never knows whether the far end is
# this container (dev) or the Proxmox LXC CT 703 (prod). Keep the two in sync at the
# level this file establishes: python3 + a build toolchain, an `agent` unix user,
# key-only sshd, /workspace. See docs/sandbox-access.md for the prod half.
#
# INVARIANT(security): only the PUBLIC key is baked here. Why: the image is
# rebuildable by anyone with the repo, so the private half would be a published
# credential; it lives only in the backend's env (SANDBOX_SSH_KEY_B64).
FROM python:3.12-slim

# WHY the toolchain: `pip install git+https://…` always builds from an sdist, and
# packages without wheels compile. The agent runs non-root and so has no apt-get —
# without gcc here it hits "command 'gcc' failed" and cannot resolve it itself.
# gcc is not an attack-surface item: the box exists to run arbitrary code and the
# Python interpreter already is that.
# util-linux is setsid: the detached-run wrapper (`sandbox_bash(detach=true)`)
# puts the background command in its own session so it survives the SSH channel.
# xz-utils is what `tar -J` shells out to — the node tarball below is .tar.xz.
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server gcc python3-dev git ca-certificates procps curl \
        util-linux xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Preinstalled so the common case needs no network round-trip; everything else is
# pip-on-demand into the per-user venv.
RUN pip install --no-cache-dir pandas numpy

# WHY node: a foreign agent CLI (the external-agent skill) is a Node package, and the
# `agent` user cannot install a runtime for itself — it is non-root with no apt-get by
# the invariant below. Only the RUNTIME is baked; the CLI is installed into the caller's
# own workspace, where npm's cache and node_modules persist, so a preview-stage
# package's churn never rides in the image.
# WHY the heap flag in the skill's install line rather than a global default here: a
# measured `npm install @deepseek-ai/dsh` OOMs node's default old-space
# (2 GB on arm64) after ~5 min and needs --max-old-space-size; forcing that ceiling on
# every node process on a SHARED box would let one run's allocator starve the others.
# Installed from the official tarball rather than Debian's `nodejs` (bookworm ships 18,
# too old) and pinned with a checksum — the box runs arbitrary code, but what it runs
# must still be the artifact we chose.
ARG NODE_VERSION=v24.19.0
ARG NODE_SHA256_amd64=14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647
ARG NODE_SHA256_arm64=01443c1e1a29e531ccad5a46fefa6df490d2189c49f7955904aecdbb0fe86fdc
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) NODE_ARCH=x64; NODE_SHA256="$NODE_SHA256_amd64" ;; \
        arm64) NODE_ARCH=arm64; NODE_SHA256="$NODE_SHA256_arm64" ;; \
        *) echo "unsupported arch: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    tarball="node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"; \
    curl -fsSLO "https://nodejs.org/dist/${NODE_VERSION}/${tarball}"; \
    echo "${NODE_SHA256}  ${tarball}" | sha256sum -c -; \
    tar -xJf "$tarball" -C /usr/local --strip-components=1 --no-same-owner \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md; \
    rm "$tarball"; \
    node --version; npm --version

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
