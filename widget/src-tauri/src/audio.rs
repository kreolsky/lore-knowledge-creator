/// macOS audio capture via Swift CLI subprocess (lore-audio-capture).
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
    /// WHY: system-audio capture is Windows-only; the flag keeps the per-OS surface identical.
    pub fn start(&self, _record_system_audio: bool) -> Result<Option<String>, String> {
        if self.is_recording() {
            return Err("Already recording".into());
        }

        let audio_file = store::new_recording_path("m4a");

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
        Ok(None)
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
