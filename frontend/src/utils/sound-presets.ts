/** Agent notification sound — the bundled preset catalogue, the persisted choice and the one player.
 * One sound for every cue (agent finished, confirmation needed). */
import { customSoundUrl, loadCustomSound } from './custom-sound';

export const SOUND_PRESETS = ['chime', 'pop', 'bell', 'soft'] as const;
export type SoundPresetId = typeof SOUND_PRESETS[number];
/** A bundled preset, or the user's own uploaded file. */
export type SoundChoice = SoundPresetId | 'custom';
export const DEFAULT_SOUND_PRESET: SoundPresetId = 'chime';

export function isSoundChoice(value: unknown): value is SoundChoice {
  return value === 'custom' || (typeof value === 'string' && (SOUND_PRESETS as readonly string[]).includes(value));
}

function play(url: string): void {
  // WHY: a rejected play() is the browser's autoplay policy (no gesture in this tab
  // yet, e.g. a turn adopted after reload) — a chime is not a user-initiated op, so
  // it is logged, not toasted.
  new Audio(url).play().catch(e => console.warn('Notification sound blocked:', e));
}

/** Play a choice's sound, ignoring the project toggle (the cabinet preview uses it directly). */
export function playSoundPreset(choice: SoundChoice): void {
  if (choice !== 'custom') { play(`/sounds/${choice}.mp3`); return; }
  const cached = customSoundUrl();
  if (cached) { play(cached); return; }
  // WHY default on a missing file: a notification that falls silent is worse than the
  // stock chime — the agent still finished. Logged, not toasted (not a user op).
  loadCustomSound().then(
    sound => {
      if (sound) { play(sound.dataUrl); return; }
      console.warn('Custom notification sound not stored, playing the default');
      play(`/sounds/${DEFAULT_SOUND_PRESET}.mp3`);
    },
    e => {
      console.warn('Custom notification sound failed to load, playing the default:', e);
      play(`/sounds/${DEFAULT_SOUND_PRESET}.mp3`);
    },
  );
}
