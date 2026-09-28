/// Lore Recorder — macOS menu bar widget for voice-to-text.
///
/// Left-click tray icon to toggle recording. Right-click for history menu.
/// Audio captured via Swift ScreenCaptureKit helper, uploaded to Lore backend
/// for transcription, result copied to clipboard automatically.

mod api;
mod audio;
mod store;

use std::sync::{Arc, Mutex};
use tauri::{
    image::Image,
    menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent,
};

struct AppState {
    recorder: audio::AudioRecorder,
    config: Mutex<store::Config>,
}

// AppState is Send+Sync: AudioRecorder has Mutex fields, config is Mutex-wrapped.
unsafe impl Send for AppState {}
unsafe impl Sync for AppState {}

/// Tauri command: get stored API key.
#[tauri::command]
fn get_api_key(state: tauri::State<'_, Arc<AppState>>) -> String {
    state.config.lock().unwrap().api_key_encoded.clone()
}

/// Tauri command: save API key.
#[tauri::command]
fn set_api_key(state: tauri::State<'_, Arc<AppState>>, key: String) -> Result<(), String> {
    store::decode_api_key(&key).ok_or("Invalid key format")?;
    let mut config = state.config.lock().unwrap();
    config.api_key_encoded = key;
    store::save_config(&config);
    Ok(())
}

#[tauri::command]
fn get_retention_days(state: tauri::State<'_, Arc<AppState>>) -> u32 {
    state.config.lock().unwrap().retention_days
}

#[tauri::command]
fn set_retention_days(state: tauri::State<'_, Arc<AppState>>, days: u32) -> Result<(), String> {
    if days == 0 || days > 365 {
        return Err("Retention days must be 1-365".into());
    }
    let mut config = state.config.lock().unwrap();
    config.retention_days = days;
    store::save_config(&config);
    Ok(())
}

static MENU_GEN: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
static EXPLICIT_EXIT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Build the right-click context menu with history entries.
fn build_tray_menu<M: tauri::Manager<tauri::Wry>>(manager: &M, config: &store::Config) -> tauri::menu::Menu<tauri::Wry> {
    let gen = MENU_GEN.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let mut builder = MenuBuilder::new(manager);

    if config.history.is_empty() {
        builder = builder.item(
            &MenuItemBuilder::with_id(format!("no-history-{gen}"), "No transcriptions yet")
                .enabled(false)
                .build(manager)
                .unwrap(),
        );
    } else {
        for (i, entry) in config.history.iter().enumerate() {
            let truncated: String = entry.text.chars().take(60).collect();
            let label = if entry.text.chars().count() > 60 {
                format!("{truncated}…")
            } else {
                truncated
            };
            builder = builder.item(
                &MenuItemBuilder::with_id(format!("history-{i}-{gen}"), label)
                    .build(manager)
                    .unwrap(),
            );
        }
    }

    builder = builder.item(&PredefinedMenuItem::separator(manager).unwrap());
    builder = builder.item(
        &MenuItemBuilder::with_id(format!("settings-{gen}"), "Settings…")
            .build(manager)
            .unwrap(),
    );
    builder = builder.item(
        &MenuItemBuilder::with_id(format!("quit-{gen}"), "Quit")
            .build(manager)
            .unwrap(),
    );

    builder.build().unwrap()
}

/// Open settings window (or focus if already open).
fn open_settings(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("settings") {
        win.set_focus().ok();
        return;
    }
    let url = tauri::WebviewUrl::App("index.html".into());
    eprintln!("[lore] Opening settings window with url: {:?}", url);
    match tauri::WebviewWindowBuilder::new(app, "settings", url)
        .title("Lore Recorder Settings")
        .inner_size(360.0, 280.0)
        .resizable(false)
        .build()
    {
        Ok(_) => eprintln!("[lore] Settings window opened"),
        Err(e) => eprintln!("[lore] Settings window error: {e}"),
    }
}

/// Main recording toggle: start or stop, then upload + poll + clipboard.
async fn toggle_recording(app: AppHandle) {
    eprintln!("[lore] toggle_recording called");
    let state = app.state::<Arc<AppState>>();
    let is_recording = state.recorder.is_recording();

    if !is_recording {
        // Start recording — runs blocking I/O in a separate thread
        eprintln!("[lore] Starting recording...");
        update_tray_icon(&app, true);
        let recorder = state.recorder.clone_arc();
        let start_result = tokio::task::spawn_blocking(move || {
            recorder.start()
        }).await.unwrap_or_else(|e| Err(format!("spawn_blocking failed: {e}")));
        if let Err(e) = start_result {
            eprintln!("[lore] Failed to start recording: {e}");
            update_tray_icon(&app, false);
            show_notification(&app, "Recording failed", &e);
            return;
        }
        eprintln!("[lore] Recording started OK");
    } else {
        // Stop recording
        update_tray_icon(&app, false);

        let recorder = state.recorder.clone_arc();
        let (audio_bytes, audio_path) = match tokio::task::spawn_blocking(move || {
            recorder.stop()
        }).await.unwrap_or_else(|e| Err(format!("spawn_blocking failed: {e}"))) {
            Ok(result) => result,
            Err(e) => {
                eprintln!("[lore] Failed to stop recording: {e}");
                show_notification(&app, "Recording failed", &e);
                return;
            }
        };
        eprintln!("[lore] Recording stopped, {} bytes, saved to {:?}", audio_bytes.len(), audio_path);

        // Get API key
        let (base_url, token) = {
            let config = state.config.lock().unwrap();
            match store::decode_api_key(&config.api_key_encoded) {
                Some(pair) => pair,
                None => {
                    show_notification(&app, "No API key", "Configure your API key in Settings");
                    open_settings(&app);
                    return;
                }
            }
        };

        // Upload
        let api = api::WidgetApi::new(&base_url, &token);
        let filename = audio_path.file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| "widget-recording.m4a".to_string());

        eprintln!("[lore] Uploading to {base_url}...");
        let upload_resp = match api.upload_audio(audio_bytes, &filename).await {
            Ok(r) => {
                eprintln!("[lore] Uploaded, ref_id={}", r.reference_id);
                r
            }
            Err(e) => {
                eprintln!("[lore] Upload failed: {e}");
                show_notification(&app, "Upload failed", &e);
                return;
            }
        };

        // Poll for transcription
        let reference_id = upload_resp.reference_id;
        let mut attempts = 0;
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            attempts += 1;
            if attempts > 900 {
                show_notification(&app, "Timeout", "Transcription took too long");
                return;
            }

            match api.poll_status(&reference_id).await {
                Ok(status) => {
                    let st = status.status.as_deref().unwrap_or("");
                    if st == "ready" {
                        let text = status.content.unwrap_or_default();
                        eprintln!("[lore] Transcription text: {text}");
                        if !text.is_empty() {
                            copy_to_clipboard(&text);
                            show_notification(
                                &app,
                                "Copied to clipboard",
                                &truncate(&text, 80),
                            );

                            // Save to history
                            let mut config = state.config.lock().unwrap();
                            store::add_history_entry(
                                &mut config,
                                store::HistoryEntry {
                                    text,
                                    timestamp: simple_timestamp(),
                                    reference_id: reference_id.clone(),
                                },
                            );
                            store::save_config(&config);
                            drop(config);

                            // Rebuild tray menu
                            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                                if let Some(tray) = app.tray_by_id("main-tray") {
                                    let config_snap = state.config.lock().unwrap().clone();
                                    let menu = build_tray_menu(&app, &config_snap);
                                    tray.set_menu(Some(menu)).ok();
                                }
                            })) {
                                Ok(_) => eprintln!("[lore] Tray menu rebuilt"),
                                Err(e) => eprintln!("[lore] Tray menu rebuild failed: {:?}", e),
                            }
                        }
                        return;
                    } else if st == "error" {
                        show_notification(
                            &app,
                            "Transcription error",
                            "Server failed to transcribe",
                        );
                        return;
                    }
                }
                Err(e) => {
                    eprintln!("Poll error: {e}");
                    show_notification(&app, "Poll failed", &e);
                    return;
                }
            }
        }
    }
}

fn update_tray_icon(app: &AppHandle, recording: bool) {
    if let Some(tray) = app.tray_by_id("main-tray") {
        let icon = if recording {
            Image::from_bytes(include_bytes!("../icons/tray-recording.png"))
        } else {
            Image::from_bytes(include_bytes!("../icons/tray-idle.png"))
        };
        if let Ok(icon) = icon {
            // ARCH: set icon first, then toggle template mode so macOS
            // re-renders with the correct mode applied to the new image.
            // template=true when idle (auto black/white for dark/light bar),
            // template=false when recording (preserves red color).
            tray.set_icon(Some(icon)).ok();
            tray.set_icon_as_template(!recording).ok();
        }
        let tooltip = if recording {
            "Lore Recorder — RECORDING (click to stop)"
        } else {
            "Lore Recorder — click to record"
        };
        tray.set_tooltip(Some(tooltip)).ok();
    }
}

fn copy_to_clipboard(text: &str) {
    use std::io::Write;
    if let Ok(mut child) = std::process::Command::new("pbcopy")
        .env("LANG", "en_US.UTF-8")
        .env("__CF_USER_TEXT_ENCODING", "0x1F5:0x08000100:0x08000100")
        .stdin(std::process::Stdio::piped())
        .spawn()
    {
        if let Some(ref mut stdin) = child.stdin {
            stdin.write_all(text.as_bytes()).ok();
        }
        child.wait().ok();
    }
}

fn show_notification(_app: &AppHandle, title: &str, body: &str) {
    std::process::Command::new("osascript")
        .arg("-e")
        .arg(format!(
            "display notification \"{}\" with title \"{}\"",
            body.replace('"', "\\\""),
            title.replace('"', "\\\"")
        ))
        .spawn()
        .ok();
}

fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_string()
    } else {
        let t: String = s.chars().take(max).collect();
        format!("{t}…")
    }
}

fn simple_timestamp() -> String {
    use std::time::SystemTime;
    let secs = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    // Minimal ISO timestamp without chrono
    let s = secs % 60;
    let m = (secs / 60) % 60;
    let h = (secs / 3600) % 24;
    format!("{secs}-{h:02}{m:02}{s:02}")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let config = store::load_config();

    // Cleanup old recordings on startup
    store::cleanup_old_recordings(config.retention_days);

    // Log orphaned recordings (files from crashed sessions)
    let orphans = store::find_orphaned_recordings();
    if !orphans.is_empty() {
        eprintln!("[lore] Found {} orphaned recording(s) in {:?}", orphans.len(), store::recordings_dir());
    }

    let state = Arc::new(AppState {
        recorder: audio::AudioRecorder::new(),
        config: Mutex::new(config),
    });

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(state.clone())
        .invoke_handler(tauri::generate_handler![get_api_key, set_api_key, get_retention_days, set_retention_days])
        .setup(move |app| {
            let config = state.config.lock().unwrap().clone();
            let menu = build_tray_menu(app, &config);
            let idle_icon =
                Image::from_bytes(include_bytes!("../icons/tray-idle.png"))?;

            let _tray = TrayIconBuilder::with_id("main-tray")
                .icon(idle_icon)
                .icon_as_template(true)
                .menu(&menu)
                .show_menu_on_left_click(false)
                .tooltip("Lore Recorder — click to record")
                .on_tray_icon_event({
                    let app = app.handle().clone();
                    move |_tray, event| {
                        if let TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        } = event
                        {
                            let app = app.clone();
                            tauri::async_runtime::spawn(async move {
                                toggle_recording(app).await;
                            });
                        }
                    }
                })
                .on_menu_event({
                    let state = state.clone();
                    move |app, event| {
                        let id = event.id().as_ref();
                        if id.starts_with("quit") {
                            EXPLICIT_EXIT.store(true, std::sync::atomic::Ordering::Relaxed);
                            app.exit(0);
                        } else if id.starts_with("settings") {
                            open_settings(app);
                        } else if id.starts_with("history-") {
                            // Format: "history-{idx}-{gen}"
                            let parts: Vec<&str> = id.splitn(3, '-').collect();
                            if let Some(idx_str) = parts.get(1) {
                                if let Ok(idx) = idx_str.parse::<usize>() {
                                    let config = state.config.lock().unwrap();
                                    if let Some(entry) = config.history.get(idx) {
                                        copy_to_clipboard(&entry.text);
                                        show_notification(
                                            app,
                                            "Copied",
                                            &truncate(&entry.text, 60),
                                        );
                                    }
                                }
                            }
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app, event| {
            if let RunEvent::ExitRequested { api, .. } = event {
                if EXPLICIT_EXIT.load(std::sync::atomic::Ordering::Relaxed) {
                    return;
                }
                api.prevent_exit();
            }
        });
}
