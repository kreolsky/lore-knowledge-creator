/// Windows audio capture: in-process WASAPI (cpal) → Ogg/Opus in ~/.lore-widget/recordings/.
///
/// Same public surface as the macOS `audio.rs` (new / clone_arc / is_recording / start / stop),
/// selected by `#[path]` in lib.rs.
///
/// The microphone is mixed with a WASAPI loopback of the default output (the other side of a
/// call) into one mono track, unless system audio is switched off in Settings.
///
/// WHY: a dedicated thread owns the cpal Streams — a Stream is not guaranteed Send, so it is
/// built, played and dropped on the same thread. The realtime callbacks only downmix and
/// send samples over a channel; resampling, mixing and encoding run on that thread, never in a callback.

use crate::ogg_opus::{self, OggOpusWriter};
use crate::store;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use rubato::Resampler;
use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

enum Msg {
    Mic(Vec<f32>),
    /// The microphone stream died mid-recording (e.g. the headset was unplugged).
    Failed(String),
    /// Loopback samples; `gen` identifies the loopback stream that produced them.
    System { gen: u32, samples: Vec<f32> },
    /// The loopback stream died; the error is logged by its callback.
    SystemFailed { gen: u32 },
    Stop,
}

struct Session {
    tx: mpsc::Sender<Msg>,
    handle: JoinHandle<Result<(), String>>,
    path: PathBuf,
}

#[derive(Clone)]
pub struct AudioRecorder {
    session: Arc<Mutex<Option<Session>>>,
}

impl AudioRecorder {
    pub fn new() -> Self {
        Self { session: Arc::new(Mutex::new(None)) }
    }

    /// Get a cloneable handle for spawn_blocking.
    pub fn clone_arc(&self) -> Self {
        self.clone()
    }

    pub fn is_recording(&self) -> bool {
        self.session.lock().unwrap().is_some()
    }

    /// Start recording the default microphone, mixed with system playback when
    /// `record_system_audio`; returns once the streams are playing.
    /// `Ok(Some(warning))` = recording the microphone only because system audio failed to open.
    pub fn start(&self, record_system_audio: bool) -> Result<Option<String>, String> {
        if self.is_recording() {
            return Err("Already recording".into());
        }
        let path = store::new_recording_path("ogg");
        let (tx, rx) = mpsc::channel::<Msg>();
        let (ready_tx, ready_rx) = mpsc::channel::<Result<Option<String>, String>>();
        let (thread_path, thread_tx) = (path.clone(), tx.clone());
        let handle = std::thread::spawn(move || capture(thread_path, thread_tx, rx, ready_tx, record_system_audio));

        let warning = match ready_rx.recv() {
            Ok(Ok(warning)) => warning,
            Ok(Err(e)) => return Err(e),
            Err(_) => return Err(join_error(handle)),
        };
        *self.session.lock().unwrap() = Some(Session { tx, handle, path });
        Ok(warning)
    }

    /// Stop recording and return (audio_bytes, audio_path).
    /// File is NOT deleted — cleanup handled by retention policy.
    pub fn stop(&self) -> Result<(Vec<u8>, PathBuf), String> {
        let session = self.session.lock().unwrap().take().ok_or("Not recording")?;
        session.tx.send(Msg::Stop).ok();
        session.handle.join().map_err(|_| "Audio thread panicked".to_string())??;

        let audio_bytes = std::fs::read(&session.path)
            .map_err(|e| format!("Failed to read audio file: {e}"))?;
        // The two Ogg header pages alone are ~100 bytes.
        if audio_bytes.len() < 200 {
            return Err("Recording too short or empty".into());
        }
        Ok((audio_bytes, session.path))
    }
}

fn join_error(handle: JoinHandle<Result<(), String>>) -> String {
    match handle.join() {
        Ok(Err(e)) => e,
        _ => "Audio thread exited before the stream started".into(),
    }
}

/// Audio thread body: open the file and devices, encode until Stop, drain, close the streams.
fn capture(
    path: PathBuf,
    tx: mpsc::Sender<Msg>,
    rx: mpsc::Receiver<Msg>,
    ready_tx: mpsc::Sender<Result<Option<String>, String>>,
    record_system_audio: bool,
) -> Result<(), String> {
    let opened = OggOpusWriter::create(&path).and_then(|writer| {
        let (stream, rate) = open_mic(tx.clone())?;
        Ok((writer, stream, To48k::new(rate)?))
    });
    let (mut writer, stream, mut resample) = match opened {
        Ok(parts) => parts,
        Err(e) => {
            ready_tx.send(Err(e.clone())).ok();
            return Err(e);
        }
    };
    let (mut system, warning) = match record_system_audio {
        true => {
            let (system, warning) = SystemAudio::start(tx);
            (Some(system), warning)
        }
        false => (None, None),
    };
    ready_tx.send(Ok(warning)).ok();

    let mut failure = None;
    loop {
        match rx.recv() {
            Ok(Msg::Mic(samples)) => write_mic(&mut writer, &mut resample, system.as_mut(), &samples)?,
            Ok(Msg::Failed(e)) => {
                failure = Some(e);
                break;
            }
            Ok(Msg::Stop) | Err(_) => break,
            Ok(other) => {
                if let Some(s) = system.as_mut() {
                    s.handle(other);
                }
            }
        }
    }
    drop(stream);
    if let Some(s) = system.as_mut() {
        s.live = None;
    }
    while let Ok(msg) = rx.try_recv() {
        match msg {
            Msg::Mic(samples) => {
                let mut chunk = resample.push(&samples)?;
                if let Some(s) = system.as_mut() {
                    s.mix_into(&mut chunk);
                }
                writer.write(&chunk)?;
            }
            other => {
                if let Some(s) = system.as_mut() {
                    s.handle(other);
                }
            }
        }
    }
    writer.finish()?;
    // WHY: a dead microphone is reported, never uploaded as a complete recording — the
    // audio up to the failure stays on disk, and the message says where.
    match failure {
        Some(e) => Err(format!("Microphone lost during recording: {e}. Audio up to that point: {}", path.display())),
        None => Ok(()),
    }
}

/// Resample a mic packet to 48 kHz, mix system audio into it, encode.
fn write_mic(
    writer: &mut OggOpusWriter,
    resample: &mut To48k,
    system: Option<&mut SystemAudio>,
    samples: &[f32],
) -> Result<(), String> {
    let mut chunk = resample.push(samples)?;
    if let Some(system) = system {
        system.mix_into(&mut chunk);
        system.check_device();
    }
    writer.write(&chunk)
}

/// Open and start the default microphone; returns the stream and its sample rate.
fn open_mic(tx: mpsc::Sender<Msg>) -> Result<(cpal::Stream, u32), String> {
    let device = cpal::default_host()
        .default_input_device()
        .ok_or("No microphone found (check Windows Settings → Privacy → Microphone)")?;
    let supported = device
        .default_input_config()
        .map_err(|e| format!("Microphone config error: {e}"))?;
    let err_tx = tx.clone();
    let stream = open_on(
        &device,
        supported.clone(),
        move |samples| { tx.send(Msg::Mic(samples)).ok(); },
        move |e| {
            eprintln!("[lore] Microphone stream error: {e}");
            err_tx.send(Msg::Failed(e)).ok();
        },
        "microphone",
    )?;
    Ok((stream, supported.sample_rate().0))
}

/// Open and start a loopback capture of the default output device.
/// Returns the stream, its sample rate and the device name (for change detection).
fn open_loopback(tx: mpsc::Sender<Msg>, gen: u32) -> Result<(cpal::Stream, u32, String), String> {
    let device = cpal::default_host()
        .default_output_device()
        .ok_or("no output device")?;
    let name = device.name().map_err(|e| format!("output device name: {e}"))?;
    let supported = device
        .default_output_config()
        .map_err(|e| format!("output config error: {e}"))?;
    let err_tx = tx.clone();
    // WHY: cpal's WASAPI host sets AUDCLNT_STREAMFLAGS_LOOPBACK when an input stream is
    // built on a render (output) device — that is what makes this a loopback capture.
    let stream = open_on(
        &device,
        supported.clone(),
        move |samples| { tx.send(Msg::System { gen, samples }).ok(); },
        move |error| {
            eprintln!("[lore] System audio stream error: {error}");
            err_tx.send(Msg::SystemFailed { gen }).ok();
        },
        "system audio",
    )?;
    Ok((stream, supported.sample_rate().0, name))
}

/// Build and play an input stream on `device` in its native sample format.
fn open_on(
    device: &cpal::Device,
    supported: cpal::SupportedStreamConfig,
    on_data: impl FnMut(Vec<f32>) + Send + 'static,
    on_error: impl FnMut(String) + Send + 'static,
    what: &str,
) -> Result<cpal::Stream, String> {
    let format = supported.sample_format();
    let config: cpal::StreamConfig = supported.into();
    let stream = match format {
        cpal::SampleFormat::F32 => build::<f32>(device, &config, on_data, on_error),
        cpal::SampleFormat::I16 => build::<i16>(device, &config, on_data, on_error),
        cpal::SampleFormat::U16 => build::<u16>(device, &config, on_data, on_error),
        cpal::SampleFormat::I32 => build::<i32>(device, &config, on_data, on_error),
        other => return Err(format!("Unsupported {what} sample format: {other:?}")),
    }
    .map_err(|e| format!("Failed to open {what}: {e}"))?;
    stream.play().map_err(|e| format!("Failed to start {what}: {e}"))?;
    Ok(stream)
}

/// Build an input stream that downmixes each frame to mono and hands it to `on_data`.
fn build<T>(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    mut on_data: impl FnMut(Vec<f32>) + Send + 'static,
    mut on_error: impl FnMut(String) + Send + 'static,
) -> Result<cpal::Stream, cpal::BuildStreamError>
where
    T: cpal::SizedSample,
    f32: cpal::FromSample<T>,
{
    let channels = config.channels as usize;
    device.build_input_stream(
        config,
        move |data: &[T], _: &cpal::InputCallbackInfo| {
            let mono: Vec<f32> = data
                .chunks(channels)
                .map(|frame| {
                    let sum: f32 = frame.iter().map(|s| <f32 as cpal::FromSample<T>>::from_sample_(*s)).sum();
                    sum / channels as f32
                })
                .collect();
            on_data(mono);
        },
        move |e| on_error(e.to_string()),
        None,
    )
}

/// System playback mixed into the microphone track; never ends the recording.
struct SystemAudio {
    tx: mpsc::Sender<Msg>,
    live: Option<Loopback>,
    /// Generation of the current loopback stream; packets of an older one are dropped.
    gen: u32,
    /// Resampled 48 kHz system samples waiting for mic samples to mix into.
    buf: VecDeque<f32>,
    last_check: Instant,
}

struct Loopback {
    _stream: cpal::Stream,
    device_name: String,
    resample: To48k,
}

/// Drift guard: more buffered system audio than this (1 s at 48 kHz) drops the oldest excess.
const MAX_SYSTEM_BUFFER: usize = 48_000;
const DEVICE_CHECK_INTERVAL: Duration = Duration::from_secs(1);

impl SystemAudio {
    /// Open the loopback; on failure returns the user-facing warning and retries on device checks.
    fn start(tx: mpsc::Sender<Msg>) -> (Self, Option<String>) {
        let mut system = Self { tx, live: None, gen: 0, buf: VecDeque::new(), last_check: Instant::now() };
        let warning = system
            .reopen()
            .err()
            .map(|e| format!("System audio unavailable: {e}. Recording the microphone only until it becomes available."));
        (system, warning)
    }

    fn reopen(&mut self) -> Result<(), String> {
        self.live = None;
        self.buf.clear();
        self.gen += 1;
        let (stream, rate, device_name) = open_loopback(self.tx.clone(), self.gen)?;
        eprintln!("[lore] System audio: capturing {device_name} at {rate} Hz");
        self.live = Some(Loopback { _stream: stream, device_name, resample: To48k::new(rate)? });
        Ok(())
    }

    fn handle(&mut self, msg: Msg) {
        match msg {
            Msg::System { gen, samples } if gen == self.gen => self.push(&samples),
            Msg::SystemFailed { gen } if gen == self.gen => self.live = None,
            _ => {}
        }
    }

    fn push(&mut self, samples: &[f32]) {
        let Some(live) = self.live.as_mut() else { return };
        match live.resample.push(samples) {
            Ok(out) => self.buf.extend(out),
            Err(e) => {
                eprintln!("[lore] System audio resample error: {e}");
                self.live = None;
            }
        }
        let excess = self.buf.len().saturating_sub(MAX_SYSTEM_BUFFER);
        self.buf.drain(..excess);
    }

    /// Add buffered system samples to a 48 kHz mic chunk, sample for sample.
    fn mix_into(&mut self, mic: &mut [f32]) {
        for s in mic.iter_mut() {
            // INVARIANT: the microphone is the clock — mixing never waits for system audio.
            // Why: WASAPI loopback delivers no packets while nothing plays, so missing system
            // samples are silence, not a reason to hold the mic back.
            let sys = self.buf.pop_front().unwrap_or(0.0);
            *s = (*s + sys).clamp(-1.0, 1.0);
        }
    }

    /// At most once per second: reopen when the loopback died or the default output changed.
    /// WHY: a default-output switch (headphones plugged in) does not error the old loopback —
    /// it keeps capturing the now-silent previous device.
    fn check_device(&mut self) {
        if self.last_check.elapsed() < DEVICE_CHECK_INTERVAL {
            return;
        }
        self.last_check = Instant::now();
        let current = cpal::default_host().default_output_device().and_then(|d| d.name().ok());
        let open = self.live.as_ref().map(|l| &l.device_name);
        if current.is_some() && current.as_ref() == open {
            return;
        }
        let was_live = self.live.is_some();
        if let Err(e) = self.reopen() {
            if was_live {
                eprintln!("[lore] System audio lost, retrying every second: {e}");
            }
        }
    }
}

/// Converts the device rate to Opus's 48 kHz; a pass-through when the device already runs at 48 kHz.
struct To48k {
    inner: Option<rubato::FftFixedIn<f32>>,
    pending: Vec<f32>,
}

impl To48k {
    fn new(rate: u32) -> Result<Self, String> {
        let inner = if rate == ogg_opus::SAMPLE_RATE {
            None
        } else {
            let r = rubato::FftFixedIn::new(rate as usize, ogg_opus::SAMPLE_RATE as usize, 1024, 2, 1)
                .map_err(|e| format!("Resampler for {rate} Hz: {e}"))?;
            Some(r)
        };
        Ok(Self { inner, pending: Vec::new() })
    }

    /// Feed device-rate samples; returns whatever 48 kHz output whole chunks produced.
    /// WHY: the sub-chunk tail left at Stop (< ~25 ms) is dropped, not padded.
    fn push(&mut self, samples: &[f32]) -> Result<Vec<f32>, String> {
        let Some(r) = self.inner.as_mut() else { return Ok(samples.to_vec()) };
        self.pending.extend_from_slice(samples);
        let mut out = Vec::new();
        while self.pending.len() >= r.input_frames_next() {
            let chunk: Vec<f32> = self.pending.drain(..r.input_frames_next()).collect();
            let resampled = r.process(&[chunk], None).map_err(|e| format!("Resample: {e}"))?;
            out.extend_from_slice(&resampled[0]);
        }
        Ok(out)
    }
}
