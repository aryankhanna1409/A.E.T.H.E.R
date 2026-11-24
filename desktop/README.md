# AETHER desktop app (Tauri 2)

A native Windows window around the local AETHER UI. It starts the Python
server in the background, waits for it, and shows the interface without a
browser. Everything still runs on your PC.

## Build (Windows 11)

1. Install Rust: https://rustup.rs (choose the MSVC toolchain) and the "Desktop development with C++" workload from Visual Studio Build Tools.
2. Install the Tauri CLI: `cargo install tauri-cli --version "^2"`
3. Make sure AETHER itself is set up (`scripts\setup_windows.ps1`) so `.venv\Scripts\aether-server.exe` exists.
4. Put a 512x512 `icon.png` in `desktop/src-tauri/icons/`, then run `cargo tauri icon desktop/src-tauri/icons/icon.png`.
5. From `desktop/`: `cargo tauri dev` to run, `cargo tauri build` for an installer (.msi) in `src-tauri/target/release/bundle`.

The app looks for the server at `..\.venv\Scripts\aether-server.exe` relative to the repo,
or at the path in the `AETHER_SERVER` environment variable.
