/// Audio capture via Swift CLI subprocess (lore-audio-capture).
///
/// AVAudioRecorder writes M4A (AAC) directly to ~/.lore-widget/recordings/.
/// Files persist on disk even if the app crashes — they can be retried on next launch.

use crate::store;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::Arc;

#[derive(Clone)]
pub struct AudioRecorder {
    inner: Arc<AudioRecorderInner>,
}

struct AudioRecorderInner {
    child_pid: Mutex<Option<u32>>,
    audio_path: Mutex<Option<PathBuf>>,
    recording: Mutex<bool>,
}

impl AudioRecorder {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(AudioRecorderInner {
                child_pid: Mutex::new(None),
                audio_path: Mutex::new(None),
                recording: Mutex::new(false),
            }),
        }
    }

    /// Get a cloneable handle for spawn_blocking.
    pub fn clone_arc(&self) -> Self {
        self.clone()
    }

    pub fn is_recording(&self) -> bool {
        *self.inner.recording.lock().unwrap()
    }

    /// Current recording file path (for crash recovery).
    pub fn current_audio_path(&self) -> Option<PathBuf> {
        self.inner.audio_path.lock().unwrap().clone()
    }

    /// Path to the bundled or development Swift audio helper.
    fn helper_path() -> PathBuf {
        if let Ok(exe) = std::env::current_exe() {
            // .app bundle: Contents/MacOS/binary → Contents/Resources/lore-audio-capture
            let resources = exe.parent().unwrap().parent().unwrap()
                .join("Resources").join("lore-audio-capture");
            if resources.exists() {
                return resources;
            }
        }
        // Dev: swift build output (try release first, then debug)
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        for profile in ["debug", "release"] {
            let path = manifest.join(format!("swift-audio/.build/{profile}/lore-audio-capture"));
            if path.exists() {
                return path;
            }
        }
        PathBuf::from("lore-audio-capture")
    }

    /// Start recording microphone audio to ~/.lore-widget/recordings/.
    pub fn start(&self) -> Result<(), String> {
        if self.is_recording() {
            return Err("Already recording".into());
        }

        let recordings = store::recordings_dir();
        let timestamp = chrono_compact_now();
        let audio_file = recordings.join(format!("widget-{timestamp}.m4a"));

        let helper = Self::helper_path();
        let mut child = std::process::Command::new(&helper)
            .arg("start")
            .arg(audio_file.to_str().unwrap())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .map_err(|e| format!("Failed to start audio helper at {:?}: {e}", helper))?;

        let stdout = child.stdout.take().ok_or("No stdout")?;
        let mut reader = std::io::BufReader::new(stdout);
        let mut pid_line = String::new();
        std::io::BufRead::read_line(&mut reader, &mut pid_line)
            .map_err(|e| format!("Failed to read PID: {e}"))?;

        let pid: u32 = pid_line.trim().parse()
            .map_err(|e| format!("Invalid PID '{pid_line}': {e}"))?;

        *self.inner.child_pid.lock().unwrap() = Some(pid);
        *self.inner.audio_path.lock().unwrap() = Some(audio_file);
        *self.inner.recording.lock().unwrap() = true;

        std::thread::spawn(move || { let _ = child.wait(); });
        Ok(())
    }

    /// Stop recording and return (audio_bytes, audio_path).
    /// File is NOT deleted — cleanup handled by retention policy.
    pub fn stop(&self) -> Result<(Vec<u8>, PathBuf), String> {
        if !self.is_recording() {
            return Err("Not recording".into());
        }

        let pid = self.inner.child_pid.lock().unwrap().take()
            .ok_or("No child PID")?;
        let audio_path = self.inner.audio_path.lock().unwrap().take()
            .ok_or("No audio path")?;

        *self.inner.recording.lock().unwrap() = false;

        unsafe { libc::kill(pid as i32, libc::SIGUSR1); }

        // Wait for helper to finalize the M4A
        std::thread::sleep(std::time::Duration::from_millis(500));

        let audio_bytes = std::fs::read(&audio_path)
            .map_err(|e| format!("Failed to read audio file: {e}"))?;

        if audio_bytes.len() < 8 {
            return Err("Recording too short or empty".into());
        }

        Ok((audio_bytes, audio_path))
    }
}

/// Format matching frontend voice recordings: "2026-03-18 04:02"
fn chrono_compact_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
    let m = (secs / 60) % 60;
    let h = (secs / 3600) % 24;
    let days = secs / 86400;
    let (y, mo, d) = civil_from_days(days as i64);
    format!("{y:04}-{mo:02}-{d:02} {h:02}:{m:02}")
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
