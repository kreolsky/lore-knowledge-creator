#!/usr/bin/env bash
# Build the Windows installer of Lore Recorder (NSIS setup.exe) inside a Linux container.
# Output: src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis/*-setup.exe
set -euo pipefail
cd "$(dirname "$0")"
docker build -t lore-widget-windows-builder - < windows.Dockerfile
# WHY: libopus's cmake marks its SSE4.1 sources with MSVC /arch flags that clang-cl ignores,
# so the intrinsics fail to compile; enabling SSSE3/SSE4.1 for the whole C build fixes it.
# CL (prepended by clang-cl to every command line, like MSVC cl) because cargo-xwin overwrites CFLAGS_<target>.
# Floor: any x86-64 CPU since ~2008 (Win11 requires newer anyway).
# WHY: the build runs as root (the cargo/xwin cache volumes are root-owned, so --user would
# break them); chown hands src-tauri/ (target/, gen/, Cargo.lock) back to the invoking user
# afterwards, even when the build fails, so cleaning it needs no sudo.
docker run --rm \
  -e CL="-mssse3 -msse4.1" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  -v "$PWD":/widget \
  -v lore-widget-cargo-registry:/usr/local/cargo/registry \
  -v lore-widget-xwin-cache:/root/.cache/cargo-xwin \
  -w /widget/src-tauri \
  lore-widget-windows-builder \
  sh -c 'status=0; cargo tauri build --runner cargo-xwin --target x86_64-pc-windows-msvc --bundles nsis || status=$?; chown -R "$HOST_UID:$HOST_GID" /widget/src-tauri; exit $status'
ls -la src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis/
