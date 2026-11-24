// AETHER desktop shell: starts the local Python server and shows its UI.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;

struct Server(Mutex<Option<Child>>);

fn server_path() -> PathBuf {
    if let Ok(p) = std::env::var("AETHER_SERVER") {
        return PathBuf::from(p);
    }
    let exe = std::env::current_exe().unwrap_or_default();
    // repo/desktop/src-tauri/target/<profile>/aether-desktop.exe -> repo/.venv/Scripts/aether-server.exe
    for anc in exe.ancestors() {
        let candidate = anc.join(".venv").join("Scripts").join("aether-server.exe");
        if candidate.exists() {
            return candidate;
        }
    }
    PathBuf::from("aether-server")
}

fn main() {
    let mut cmd = Command::new(server_path());
    cmd.arg("--no-browser");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW: no console window
    }
    let child = cmd.spawn().ok();

    tauri::Builder::default()
        .manage(Server(Mutex::new(child)))
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                use tauri::Manager;
                if let Some(state) = window.try_state::<Server>() {
                    if let Some(mut c) = state.0.lock().unwrap().take() {
                        let _ = c.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running AETHER");
}
