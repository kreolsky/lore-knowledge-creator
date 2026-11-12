#!/usr/bin/env bash
# The ONE place the AGENT'S ENVIRONMENT is installed — apt toolchain, Node, the
# Python stack, Playwright. It runs as root, is idempotent, and targets the
# Debian family with apt: Debian bookworm in the sandbox image (python:3.12-slim,
# run by sandbox/Dockerfile at build time) and Ubuntu 24.04 on prod CT 703 (run
# by the prod deploy step, .gitea/workflows/deploy.yml). Playwright's
# --with-deps supports both.
#
# The host's PLUMBING stays per host and outside this script: the `agent` user,
# sshd and its config (the Dockerfile block for the image, CT 703's own for the
# box), nftables.
#
# Pinned versions live in the repo beside this script: requirements.txt (exact
# pins) and the Node version + checksums below. An update is a deliberate bump
# in a commit.
set -euo pipefail
# WHY: `pct exec` (the hand re-run path) starts with a PATH lacking /usr/local/bin,
# where the pinned node lands; the deploy's SSH login PATH already has it.
export PATH="/usr/local/sbin:/usr/local/bin:$PATH"

# WHY noninteractive + confold: the prod deploy runs this over SSH with no tty, where
# a debconf question or a modified-conffile prompt during a dependency upgrade would
# hang or fail the job; keeping the box's existing config file is the safe answer.
# WHY the lock timeout: Ubuntu's unattended-upgrades holds the dpkg lock on CT 703 at
# random times, and apt without one fails at once ("Could not get lock") — a red
# deploy for a box that only needed to wait.
export DEBIAN_FRONTEND=noninteractive
APT_LOCK=(-o DPkg::Lock::Timeout=600)
# WHY a conf.d file rather than an -o flag: `playwright install --with-deps` runs its
# own apt-get, which only a config file reaches. A transient 502 from the LAN apt
# proxy failed a prod deploy's provisioning on one .deb; apt retries it instead.
echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80lore-retries
APT_INSTALL=(apt-get "${APT_LOCK[@]}" install -y --no-install-recommends -o Dpkg::Options::=--force-confold)

die() { echo "provision: $*" >&2; exit 1; }

# ─── Preflight, named and loud, before anything is installed ───────────────────
# A failure exits non-zero with ONE line naming the requirement and the found
# version — never a pip traceback.

[ "$(id -u)" -eq 0 ] || die "must run as root (apt, /opt/ms-playwright, /etc/lore-sandbox)"
[ -f /etc/debian_version ] || die "requires an apt-based Debian-family host (/etc/debian_version not found)"
command -v python3 >/dev/null 2>&1 || die "requires python3 >= 3.11; no python3 in PATH"
FOUND_PY="$(python3 -c 'import platform; print(platform.python_version())')"
python3 - <<'PY' || die "requires python3 >= 3.11 (pandas 3.x floor); found $FOUND_PY"
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

# The script always uses `python3` from PATH — the image's /usr/local interpreter
# or CT 703's system one alike.
REQ="$(dirname "$(readlink -f "$0")")/requirements.txt"
[ -f "$REQ" ] || die "requirements.txt not found next to this script ($REQ)"

# ─── apt toolchain ─────────────────────────────────────────────────────────────
# WHY the toolchain: `pip install git+https://…` always builds from an sdist, and
# packages without wheels compile. The agent runs non-root and so has no apt-get —
# without gcc here it hits "command 'gcc' failed" and cannot resolve it itself.
# gcc is not an attack-surface item: the box exists to run arbitrary code and the
# Python interpreter already is that.
# util-linux is setsid: the detached-run wrapper (`sandbox_bash(detach=true)`)
# puts the background command in its own session so it survives the SSH channel.
# xz-utils is what `tar -J` shells out to — the node tarball below is .tar.xz.
apt-get "${APT_LOCK[@]}" update
"${APT_INSTALL[@]}" \
    ca-certificates curl xz-utils git gcc python3-dev procps util-linux

# Debian's python3-pip / python3-venv are installed ONLY when the probes fail —
# both already work on CT 703 and in python:3.12-slim today, so this is a guard
# for a fresh host; in the image an unconditional apt python3-pip would add a
# second interpreter.
python3 -m pip --version >/dev/null 2>&1 || \
    "${APT_INSTALL[@]}" python3-pip
python3 -m venv --help >/dev/null 2>&1 || \
    "${APT_INSTALL[@]}" python3-venv

# ─── Node ──────────────────────────────────────────────────────────────────────
# WHY node: a foreign agent CLI (the external-agent skill) is a Node package, and the
# `agent` user cannot install a runtime for itself — it is non-root with no apt-get by
# the image's invariant. Only the RUNTIME is baked; the CLI is installed into the caller's
# own workspace, where npm's cache and node_modules persist, so a preview-stage
# package's churn never rides in the image.
# WHY the heap flag in the skill's install line rather than a global default here: a
# measured `npm install @deepseek-ai/dsh` OOMs node's default old-space
# (2 GB on arm64) after ~5 min and needs --max-old-space-size; forcing that ceiling on
# every node process on a SHARED box would let one run's allocator starve the others.
# Installed from the official tarball rather than Debian's `nodejs` (bookworm ships 18,
# too old) and pinned with a checksum — the box runs arbitrary code, but what it runs
# must still be the artifact we chose.
NODE_VERSION=v24.19.0
NODE_SHA256_amd64=14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647
NODE_SHA256_arm64=01443c1e1a29e531ccad5a46fefa6df490d2189c49f7955904aecdbb0fe86fdc
case "$(dpkg --print-architecture)" in
    amd64) NODE_ARCH=x64; NODE_SHA256="$NODE_SHA256_amd64" ;;
    arm64) NODE_ARCH=arm64; NODE_SHA256="$NODE_SHA256_arm64" ;;
    *) echo "provision: unsupported arch: $(dpkg --print-architecture)" >&2; exit 1 ;;
esac
# WHY the version check: the prod deploy re-runs this script every release; the
# pinned tarball is re-fetched only when /usr/local's node is not already it.
if [ "$(/usr/local/bin/node --version 2>/dev/null || true)" != "$NODE_VERSION" ]; then
    tarball="node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
    curl -fsSLO "https://nodejs.org/dist/${NODE_VERSION}/${tarball}"
    echo "${NODE_SHA256}  ${tarball}" | sha256sum -c -
    tar -xJf "$tarball" -C /usr/local --strip-components=1 --no-same-owner \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md
    rm "$tarball"
fi
node --version; npm --version

# ─── Python stack (into the SYSTEM interpreter) ────────────────────────────────
# --break-system-packages: Ubuntu 24.04's python is externally managed (PEP 668);
# harmless in the image. The per-user venv is created --system-site-packages
# (block 1's run wrapper), so these are visible to every user. Preinstalled so
# the common case needs no network round-trip; everything else is pip-on-demand
# into the per-user venv.
python3 -m pip install --break-system-packages --no-cache-dir -r "$REQ"

# ─── Playwright ────────────────────────────────────────────────────────────────
# The browsers live OUTSIDE any $HOME (the default path is under the per-user
# workspace): one shared install under /opt, world-readable so the non-root
# agent can run them. --with-deps installs the browser libraries via apt.
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# WHY the dry-run probe: the prod deploy re-runs this every release, and
# --with-deps hits apt (and its proxy) even when nothing is missing. The browser
# directory exists only after its deps installed, so its presence means done;
# a Playwright pin bump changes the directory and re-installs.
pw_dir="$(python3 -m playwright install --dry-run chromium | sed -n 's/^ *Install location: *//p' | head -1)"
if [ -z "$pw_dir" ] || [ ! -d "$pw_dir" ]; then
    python3 -m playwright install --with-deps chromium
fi
chmod -R a+rX /opt/ms-playwright

# ─── The manifest: what is ACTUALLY installed ──────────────────────────────────
# Generated, never hand-written: python/node/chromium versions plus every
# requirements.txt name with its installed version, read back from the system
# interpreter's importlib.metadata — the served tool description renders from
# this (transport.sandbox_env_fill). Root-owned, world-readable.

CHROMIUM_VERSION=""
chrome_bin="$(find /opt/ms-playwright -maxdepth 3 -type f -name chrome -path '*chromium-*' | sort | head -1 || true)"
if [ -n "$chrome_bin" ]; then
    CHROMIUM_VERSION="$("$chrome_bin" --version 2>/dev/null | sed 's/^[^0-9]*//' | tr -d '[:space:]' || true)"
fi

install -d -m 0755 /etc/lore-sandbox
CHROMIUM_VERSION="$CHROMIUM_VERSION" python3 - "$REQ" > /etc/lore-sandbox/manifest.json.new <<'PY'
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys

req_path = sys.argv[1]
names = []
for line in open(req_path, encoding="utf-8"):
    line = line.split("#", 1)[0].strip()
    if line:
        names.append(line.split("==")[0].strip())

packages = {}
for name in names:
    try:
        packages[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        print(f"provision: {name} did not install into the system interpreter",
              file=sys.stderr)
        raise SystemExit(1)

node = (
    subprocess.run(["node", "--version"], capture_output=True, text=True,
                   check=True).stdout.strip()
    if shutil.which("node") else None
)

json.dump(
    {
        "python": platform.python_version(),
        "node": node,
        "chromium": os.environ.get("CHROMIUM_VERSION") or None,
        "packages": packages,
    },
    sys.stdout,
    indent=2,
)
PY
mv /etc/lore-sandbox/manifest.json.new /etc/lore-sandbox/manifest.json
chmod 0644 /etc/lore-sandbox/manifest.json

cat > /etc/lore-sandbox/env.sh.new <<'EOF'
# Generated by sandbox/provision.sh — the provisioned environment sourced by
# every sandbox run wrapper. Do not edit by hand.
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
EOF
mv /etc/lore-sandbox/env.sh.new /etc/lore-sandbox/env.sh
chmod 0644 /etc/lore-sandbox/env.sh

# The image ships without apt lists (the Dockerfile convention); on CT 703 the
# next run's apt-get update restores them.
rm -rf /var/lib/apt/lists/*

echo "provision: manifest at /etc/lore-sandbox/manifest.json"
