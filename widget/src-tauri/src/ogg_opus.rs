/// Ogg/Opus file writer (RFC 7845): mono 48 kHz speech at 32 kbit/s.
///
/// WHY: one Ogg page per second, flushed to disk — a crash mid-recording leaves a file that
/// plays up to the last second, unlike M4A whose index is written only at the end.

use ogg::writing::{PacketWriteEndInfo, PacketWriter};
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

pub const SAMPLE_RATE: u32 = 48_000;
/// 20 ms frame at 48 kHz — the Opus default frame size.
const FRAME: usize = 960;
/// WHY: 32 kbit/s mono is transparent for speech recognition; ~14 MB per hour.
const BITRATE: i32 = 32_000;
const SERIAL: u32 = 0x4c6f_7265;
/// 50 × 20 ms = one page per second.
const PACKETS_PER_PAGE: u32 = 50;
/// Max Opus packet size recommended by libopus docs.
const MAX_PACKET: usize = 4000;

pub struct OggOpusWriter {
    ogg: PacketWriter<'static, BufWriter<File>>,
    encoder: opus::Encoder,
    pending: Vec<f32>,
    packet: Vec<u8>,
    pre_skip: u64,
    /// Samples handed to the encoder so far (48 kHz), padding excluded.
    encoded: u64,
    packets_in_page: u32,
}

fn err(e: impl std::fmt::Display) -> String {
    format!("Opus writer: {e}")
}

impl OggOpusWriter {
    pub fn create(path: &Path) -> Result<Self, String> {
        let file = File::create(path).map_err(|e| format!("Failed to create {path:?}: {e}"))?;
        let mut encoder = opus::Encoder::new(SAMPLE_RATE, opus::Channels::Mono, opus::Application::Voip)
            .map_err(err)?;
        encoder.set_bitrate(opus::Bitrate::Bits(BITRATE)).map_err(err)?;
        let pre_skip = encoder.get_lookahead().map_err(err)? as u64;

        let mut ogg = PacketWriter::new(BufWriter::new(file));
        ogg.write_packet(opus_head(pre_skip as u16), SERIAL, PacketWriteEndInfo::EndPage, 0)
            .map_err(err)?;
        ogg.write_packet(opus_tags(), SERIAL, PacketWriteEndInfo::EndPage, 0)
            .map_err(err)?;
        Ok(Self {
            ogg,
            encoder,
            pending: Vec::with_capacity(FRAME * 2),
            packet: vec![0; MAX_PACKET],
            pre_skip,
            encoded: 0,
            packets_in_page: 0,
        })
    }

    /// Append mono 48 kHz samples; whole 20 ms frames are encoded immediately.
    pub fn write(&mut self, samples: &[f32]) -> Result<(), String> {
        self.pending.extend_from_slice(samples);
        while self.pending.len() >= FRAME {
            let frame: Vec<f32> = self.pending.drain(..FRAME).collect();
            self.encoded += FRAME as u64;
            self.encode(&frame, false)?;
        }
        Ok(())
    }

    /// Encode the zero-padded tail and close the stream; the final granule position
    /// excludes the padding so players trim it.
    pub fn finish(mut self) -> Result<(), String> {
        let mut frame = std::mem::take(&mut self.pending);
        self.encoded += frame.len() as u64;
        frame.resize(FRAME, 0.0);
        self.encode(&frame, true)?;
        self.ogg.inner_mut().flush().map_err(err)
    }

    fn encode(&mut self, frame: &[f32], last: bool) -> Result<(), String> {
        let len = self.encoder.encode_float(frame, &mut self.packet).map_err(err)?;
        self.packets_in_page += 1;
        let ends_page = last || self.packets_in_page >= PACKETS_PER_PAGE;
        let info = match (last, ends_page) {
            (true, _) => PacketWriteEndInfo::EndStream,
            (false, true) => PacketWriteEndInfo::EndPage,
            (false, false) => PacketWriteEndInfo::NormalPacket,
        };
        let granule = self.pre_skip + self.encoded;
        self.ogg.write_packet(self.packet[..len].to_vec(), SERIAL, info, granule).map_err(err)?;
        if ends_page {
            self.packets_in_page = 0;
            self.ogg.inner_mut().flush().map_err(err)?;
        }
        Ok(())
    }
}

/// Identification header, RFC 7845 §5.1 (mono, channel mapping family 0).
fn opus_head(pre_skip: u16) -> Vec<u8> {
    let mut h = b"OpusHead".to_vec();
    h.push(1); // version
    h.push(1); // channels
    h.extend_from_slice(&pre_skip.to_le_bytes());
    h.extend_from_slice(&SAMPLE_RATE.to_le_bytes());
    h.extend_from_slice(&0i16.to_le_bytes()); // output gain
    h.push(0); // mapping family
    h
}

/// Comment header, RFC 7845 §5.2 (vendor string, no user comments).
fn opus_tags() -> Vec<u8> {
    let vendor = b"lore-widget";
    let mut t = b"OpusTags".to_vec();
    t.extend_from_slice(&(vendor.len() as u32).to_le_bytes());
    t.extend_from_slice(vendor);
    t.extend_from_slice(&0u32.to_le_bytes());
    t
}
