/// Local config and transcription history, persisted at ~/.lore-widget/config.json.
/// Recordings saved to ~/.lore-widget/recordings/ with auto-cleanup.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HistoryEntry {
    pub text: String,
    pub timestamp: String,
    pub reference_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub api_key_encoded: String,
    #[serde(default)]
    pub history: Vec<HistoryEntry>,
    /// Days to keep local recording files (default: 2).
    #[serde(default = "default_retention_days")]
    pub retention_days: u32,
    /// Windows: mix system playback (call participants) into the recording (default: on).
    #[serde(default = "default_true")]
    pub record_system_audio: bool,
}

fn default_retention_days() -> u32 { 2 }
fn default_true() -> bool { true }

impl Default for Config {
    fn default() -> Self {
        Self {
            api_key_encoded: String::new(),
            history: Vec::new(),
            retention_days: default_retention_days(),
            record_system_audio: true,
        }
    }
}

/// Root directory for all widget data: ~/.lore-widget/
pub fn data_dir() -> PathBuf {
    let dir = dirs::home_dir()
        .expect("No home directory")
        .join(".lore-widget");
    std::fs::create_dir_all(&dir).ok();
    dir
}

fn config_path() -> PathBuf {
    data_dir().join("config.json")
}

/// Directory for local recording files (M4A on macOS, Ogg/Opus on Windows).
pub fn recordings_dir() -> PathBuf {
    let dir = data_dir().join("recordings");
    std::fs::create_dir_all(&dir).ok();
    dir
}

pub fn load_config() -> Config {
    let path = config_path();
    if path.exists() {
        let data = std::fs::read_to_string(&path).unwrap_or_default();
        serde_json::from_str(&data).unwrap_or_default()
    } else {
        Config::default()
    }
}

pub fn save_config(config: &Config) {
    let path = config_path();
    let data = serde_json::to_string_pretty(config).expect("Failed to serialize config");
    std::fs::write(path, data).expect("Failed to write config");
}

/// Decode all-in-one key: base64(json({url, token}))
pub fn decode_api_key(encoded: &str) -> Option<(String, String)> {
    use base64::Engine;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .ok()?;
    let json: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    let url = json.get("url")?.as_str()?.to_string();
    let token = json.get("token")?.as_str()?.to_string();
    Some((url, token))
}

pub fn add_history_entry(config: &mut Config, entry: HistoryEntry) {
    config.history.insert(0, entry);
    if config.history.len() > 10 {
        config.history.truncate(10);
    }
}

/// Delete recording files older than retention_days.
pub fn cleanup_old_recordings(retention_days: u32) {
    let dir = recordings_dir();
    let cutoff = std::time::SystemTime::now()
        - std::time::Duration::from_secs(retention_days as u64 * 86400);

    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.extension().is_some_and(|ext| ext == "m4a" || ext == "wav" || ext == "ogg") {
            continue;
        }
        if let Ok(meta) = path.metadata() {
            if let Ok(modified) = meta.modified() {
                if modified < cutoff {
                    eprintln!("[lore] Cleaning up old recording: {:?}", path);
                    std::fs::remove_file(&path).ok();
                }
            }
        }
    }
}

/// Find orphaned recordings (audio files that weren't uploaded due to crash).
pub fn find_orphaned_recordings() -> Vec<PathBuf> {
    let dir = recordings_dir();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(_) => return Vec::new(),
    };
    let mut orphans = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().is_some_and(|ext| ext == "m4a" || ext == "wav" || ext == "ogg") {
            if let Ok(meta) = path.metadata() {
                if meta.len() > 8 {
                    orphans.push(path);
                }
            }
        }
    }
    orphans
}

/// A fresh recording file path: `widget-<UTC date> <HH:MM:SS>.<ext>`.
pub fn new_recording_path(ext: &str) -> PathBuf {
    recordings_dir().join(format!("widget-{}.{ext}", chrono_compact_now()))
}

/// Frontend voice-recording format plus seconds: "2026-03-18 04:02:37".
/// WHY: seconds because `File::create` truncates — a minute-resolution name let a second
/// recording in the same minute overwrite a first one still waiting for its upload retry.
/// WHY: ':' is illegal in a Windows file name, and the name becomes the
/// reference title — Windows uses "04.02.37" rather than failing to create the file.
fn chrono_compact_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
    let s = secs % 60;
    let m = (secs / 60) % 60;
    let h = (secs / 3600) % 24;
    let days = secs / 86400;
    let (y, mo, d) = civil_from_days(days as i64);
    let sep = if cfg!(windows) { '.' } else { ':' };
    format!("{y:04}-{mo:02}-{d:02} {h:02}{sep}{m:02}{sep}{s:02}")
}

/// Convert days since 1970-01-01 to (year, month, day).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097) as u32;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}
