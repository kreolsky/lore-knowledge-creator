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
}

fn default_retention_days() -> u32 { 2 }

impl Default for Config {
    fn default() -> Self {
        Self {
            api_key_encoded: String::new(),
            history: Vec::new(),
            retention_days: default_retention_days(),
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

/// Directory for local recording WAV files.
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
        if !path.extension().is_some_and(|ext| ext == "m4a" || ext == "wav") {
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
        if path.extension().is_some_and(|ext| ext == "m4a" || ext == "wav") {
            if let Ok(meta) = path.metadata() {
                if meta.len() > 8 {
                    orphans.push(path);
                }
            }
        }
    }
    orphans
}
