# Toolchain image for cross-compiling Lore Recorder to Windows (NSIS installer) on Linux.
# WHY: Tauri's documented Linux→Windows path — cargo-xwin fetches the MSVC CRT/SDK, llvm/lld
# link and compile resources, cmake builds the bundled libopus, nsis bundles. appindicator: tauri-cli probes for it whenever the
# tray-icon feature is on, even for a non-Linux target. No Windows host or runner exists in the lab.
FROM rust:1-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends nsis lld llvm clang cmake ninja-build libayatana-appindicator3-dev \
 && apt-get clean
RUN rustup target add x86_64-pc-windows-msvc
RUN cargo install --locked cargo-xwin \
 && cargo install --locked tauri-cli --version "^2"
