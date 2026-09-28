/** SYSTEM: audio-player — custom audio controls (replaces native <audio controls>).
 *
 * Why custom: the native media "⋮" / playback-speed menu lives in Chromium's
 * closed Shadow DOM — its dropdown direction is decided by the browser and
 * cannot be flipped. At the top of the viewport the native menu opens upward
 * and is clipped off-screen. Building on the shared Dropdown/Popover primitive
 * with `placement="bottom"` gives us full control of the direction.
 *
 * ARCH: keeps a hidden `<audio>` element (native decoding + events for free,
 * and preserves the SplitEditorLayout test contract that asserts an <audio> in
 * the DOM). No `controls` attribute — all UI is ours.
 * INVARIANT: no rounded corners (border-radius: 0 project rule). No silent degradation.
 * Why: a media error must surface an explicit in-bar error + toast, never a dead control
 * indistinguishable from "loading" (CLAUDE.md: no silent degradation rule).
 *
 * ARCH: seek range vs. duration. WebM/Opus recordings (this app's primary audio
 * format, produced by the audio-upload flow) report `duration === Infinity` over
 * HTTP until the whole file is buffered — gating the slider on
 * `Number.isFinite(duration)` left it PERMANENTLY disabled (the bar didn't move
 * and couldn't be grabbed). Why: derive the scrub ceiling from the finite
 * duration, falling back to `audio.seekable.end()` (the buffered range) so the
 * user can scrub the downloaded portion while the rest streams in. Never hard-
 * disable once there is any seekable content.
 * ARCH: scrubbing guard. The slider value is bound to `currentTime`, which
 * `timeupdate` advances ~4×/s during playback — without the guard that update
 * snaps the thumb back to the playback position mid-drag, making the slider
 * impossible to grab. While `seeking` (pointer down) we ignore `timeupdate` and
 * let `onChange` drive the thumb; a one-shot window `pointerup` clears the flag
 * so a release outside the track can't leave it stuck.
 * ARCH: playback rate is NOT component state — it is the project-wide last
 * choice from ./playback-rate (localStorage-backed, shared by every player,
 * survives remount / reference switch / reload). The media element is updated
 * through ONE write path: the [rate, src] effect — the HTML media load
 * algorithm resets playbackRate when a new src loads, and ReferenceMediaBar
 * reuses this instance across reference switches — plus a belt-and-suspenders
 * re-apply in onLoadedMetadata (the load can also reset it after the effect
 * ran). The rate deliberately does NOT reset on src change.
 */

import { useRef, useState, useMemo, useEffect } from 'react';
import { Play, Pause, Volume2, VolumeX, Download } from 'lucide-react';
import { IconButton, Dropdown } from '../ui';
import { formatDuration } from '../references/ref-utils';
import { useTranslation } from '../../i18n';
import { useAppStore } from '../../store/app-store';
import { RATES, usePlaybackRate, usePlaybackRateStore } from './playback-rate';

export interface AudioPlayerProps {
  /** referenceFileUrl(...) — public-share aware (/api/public/{token}/files/… on /s/:token). */
  src: string;
  /** aria-label fallback + download filename fallback. */
  title: string;
  /** Known total duration (seconds) from file_meta.duration_sec. Used as the scrub
   * ceiling BEFORE the element reports its own finite duration — a duration-less
   * WebM reports Infinity until fully buffered, so without this the bar is dead
   * on first open. The element still wins once it knows (see scrubCeiling). */
  durationSec?: number;
  /** Optional original-file download URL (public-aware fileUrl). Omit ⇒ no download button. */
  downloadUrl?: string;
  downloadName?: string;
  /** Rendered right after the download link (the reference file's delete action —
   * the download itself stays the player's own link). */
  trailing?: React.ReactNode;
}

export function AudioPlayer({ src, title, durationSec, downloadUrl, downloadName, trailing }: AudioPlayerProps) {
  const { t } = useTranslation();
  const audioRef = useRef<HTMLAudioElement>(null);

  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  // Scrub ceiling: finite duration when known, else the buffered seekable end.
  // Stays 0 until metadata/buffering gives us anything to scrub.
  const [seekMax, setSeekMax] = useState(0);
  const [seeking, setSeeking] = useState(false);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const rate = usePlaybackRate();
  const setRate = usePlaybackRateStore(s => s.setRate);
  const [error, setError] = useState<string | null>(null);

  const rateOptions = useMemo(
    () => RATES.map(r => ({ value: String(r), label: `${r}×` })),
    [],
  );

  // Finite duration if the container carries one; otherwise the known
  // durationSec prop (file_meta.duration_sec); otherwise the furthest
  // downloaded position so the user can scrub the buffered portion.
  // Order: the element still wins when it knows, so a wrong stored number can
  // never outlive the real one; durationSec lights up the bar before any
  // metadata event (the duration-less-WebM case the remux fix targets).
  const scrubCeiling = (a: HTMLAudioElement): number => {
    const d = a.duration;
    if (Number.isFinite(d) && d > 0) return d;
    if (durationSec && durationSec > 0) return durationSec;
    return a.seekable && a.seekable.length > 0 ? a.seekable.end(a.seekable.length - 1) : 0;
  };

  // Clear the scrubbing flag on pointer-up anywhere (release outside the track
  // included) so the thumb can never get stuck ignoring playback.
  useEffect(() => {
    if (!seeking) return;
    const clear = () => setSeeking(false);
    window.addEventListener('pointerup', clear, { once: true });
    return () => window.removeEventListener('pointerup', clear);
  }, [seeking]);

  // Reset the UI mirrors when the source changes. ReferenceMediaBar is rendered
  // without a key at its call sites, so switching audio references reuses THIS
  // instance — the <audio> reloads on the new src, but React state (a prior
  // load error, the old currentTime/seekMax/isPlaying) would otherwise carry
  // over and misrepresent the new reference (stale "Failed to load audio",
  // hidden seek bar). The seek ceiling adopts the NEW reference's durationSec
  // (not 0) so the bar is usable before the element reports its own duration.
  // No-op on the initial mount.
  useEffect(() => {
    setError(null);
    setCurrentTime(0);
    setSeekMax(durationSec && durationSec > 0 ? durationSec : 0);
    setIsPlaying(false);
  }, [src, durationSec]);

  // Push the shared rate onto the media element — the single write path.
  // `src` is a dep because the media load algorithm resets playbackRate when
  // a new source loads (instance is reused across reference switches without
  // a key); onLoadedMetadata below re-applies for the same reason.
  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = rate;
  }, [rate, src]);

  const togglePlay = () => {
    const a = audioRef.current;
    if (!a) return;
    if (a.paused) {
      // Autoplay policy / no source → play() rejects; swallow so it never
      // surfaces as an unhandled rejection (the error path is onError).
      a.play().catch(() => {});
    } else {
      a.pause();
    }
  };

  const toggleMute = () => {
    const a = audioRef.current;
    if (!a) return;
    const next = !a.muted;
    a.muted = next;
    setMuted(next);
  };

  const handleSeek = (e: React.SyntheticEvent<HTMLInputElement>) => {
    const a = audioRef.current;
    const v = Number(e.currentTarget.value);
    if (a) a.currentTime = v;
    setCurrentTime(v);
  };

  const handleVolume = (e: React.SyntheticEvent<HTMLInputElement>) => {
    const a = audioRef.current;
    const v = Number(e.currentTarget.value);
    if (a) a.volume = v;
    setVolume(v);
    if (v === 0) { if (a) a.muted = true; setMuted(true); }
    else if (muted) { if (a) a.muted = false; setMuted(false); }
  };

  const handleRate = (value: string) => {
    setRate(Number(value));
  };

  const handleError = () => {
    setError(t('audioLoadError'));
    useAppStore.getState().showToast(t('audioLoadError'), 'error');
  };

  return (
    <div
      className="flex items-center gap-2 w-full h-8 px-1"
      role="group"
      aria-label={title}
    >
      <IconButton
        size="sm"
        aria-label={isPlaying ? t('pause') : t('play')}
        onClick={togglePlay}
      >
        {isPlaying ? <Pause size={14} /> : <Play size={14} />}
      </IconButton>

      {error ? (
        <span role="alert" className="flex-1 text-red text-ui-xs truncate">{error}</span>
      ) : (
        <>
          <span className="text-text-muted text-ui-xs tabular-nums w-[34px] text-right">
            {formatDuration(currentTime)}
          </span>
          <input
            type="range"
            className="audio-player-range flex-1"
            min={0}
            max={seekMax}
            step={0.1}
            value={Math.min(currentTime, seekMax)}
            disabled={!!error || seekMax <= 0}
            aria-label={t('play')}
            onChange={handleSeek}
            onPointerDown={() => setSeeking(true)}
            onPointerUp={() => setSeeking(false)}
          />
          <span className="text-text-muted text-ui-xs tabular-nums w-[34px] text-right">
            {formatDuration(seekMax)}
          </span>
        </>
      )}

      <IconButton
        size="sm"
        aria-label={muted || volume === 0 ? t('unmute') : t('mute')}
        onClick={toggleMute}
      >
        {muted || volume === 0 ? <VolumeX size={14} /> : <Volume2 size={14} />}
      </IconButton>
      <input
        type="range"
        className="audio-player-range w-14"
        min={0}
        max={1}
        step={0.05}
        value={muted ? 0 : volume}
        aria-label={t('mute')}
        onChange={handleVolume}
      />

      <Dropdown
        value={String(rate)}
        options={rateOptions}
        onSelect={handleRate}
        title={t('playbackSpeed')}
        align="right"
        placement="bottom"
      />

      {downloadUrl && (
        <a
          href={downloadUrl}
          download={downloadName}
          title={t('download')}
          aria-label={t('download')}
          className="flex items-center justify-center w-[22px] h-[22px] text-text-muted hover:text-text hover:bg-surface3"
        >
          <Download size={14} />
        </a>
      )}

      {trailing}

      {/* Hidden media element — drives decoding + events; preserves the
          SplitEditorLayout <audio>-in-DOM contract. No native controls. */}
      <audio
        ref={audioRef}
        src={src}
        preload="metadata"
        aria-hidden="true"
        onLoadedMetadata={e => {
          setSeekMax(scrubCeiling(e.currentTarget));
          // Post-load reset guard: the media load algorithm can reset
          // playbackRate after the [rate, src] effect already ran.
          if (audioRef.current) audioRef.current.playbackRate = rate;
        }}
        onDurationChange={e => setSeekMax(scrubCeiling(e.currentTarget))}
        onProgress={e => setSeekMax(scrubCeiling(e.currentTarget))}
        onTimeUpdate={e => {
          // Hold the thumb where the user is scrubbing — don't let playback's
          // timeupdate snap it back mid-drag.
          if (seeking) return;
          setCurrentTime(e.currentTarget.currentTime);
          setSeekMax(scrubCeiling(e.currentTarget));
        }}
        onPlay={() => setIsPlaying(true)}
        onPause={() => setIsPlaying(false)}
        onEnded={() => setIsPlaying(false)}
        onVolumeChange={e => { setVolume(e.currentTarget.volume); setMuted(e.currentTarget.muted); }}
        onError={handleError}
      />
    </div>
  );
}
