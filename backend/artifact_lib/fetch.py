#!/usr/bin/env python3
"""Fetch the pinned artifact libraries and verify their sha256 (build-time).

Run by BOTH backend Dockerfiles after the source COPY — the artifacts are
served from a SEPARATE cookieless origin (SYSTEM: artifact-serve) and must
be self-hosted: no page may load a third-party CDN.

Files land OUTSIDE /app, in /opt/artifact_lib/<name>@<version>/<path>:
dev/gray bind-mount ./backend over /app (docker-compose.yml), so anything a
build writes under /app is shadowed by the host tree and /lib/* would 404 on
gray while working in prod. /opt is image-private and survives both shapes.

stdlib only (urllib + hashlib) — the build stage has no dependencies yet.
Exits non-zero on a download failure OR a sha256 mismatch: a tampered or
corrupt file fails the BUILD, it never ships.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

#: MUST equal artifacts.py ARTIFACT_LIB_ROOT (kept a literal here: the build
#: stage must not import app code to learn a path).
DEST_ROOT = Path("/opt/artifact_lib")

MANIFEST = Path(__file__).resolve().parent / "manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failures = 0
    for lib in manifest.get("libs", []):
        slug = f"{lib['name']}@{lib['version']}"
        for f in lib.get("files", []):
            dest = DEST_ROOT / slug / f["path"]
            if dest.is_file() and sha256_file(dest) == f["sha256"]:
                print(f"ok (cached) {slug}/{f['path']}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                print(f"fetch {f['url']}")
                with urllib.request.urlopen(f["url"], timeout=60) as resp, \
                        tmp.open("wb") as out:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
            except Exception as exc:  # noqa: BLE001 — any fetch failure is fatal
                print(f"FAIL fetch {slug}/{f['path']}: {exc}", file=sys.stderr)
                failures += 1
                tmp.unlink(missing_ok=True)
                continue
            got = sha256_file(tmp)
            if got != f["sha256"]:
                print(
                    f"FAIL sha256 {slug}/{f['path']}: expected {f['sha256']}, got {got}",
                    file=sys.stderr,
                )
                failures += 1
                tmp.unlink(missing_ok=True)
                continue
            tmp.replace(dest)
            print(f"ok {slug}/{f['path']}")
    if failures:
        print(f"{failures} artifact library file(s) failed verification", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
