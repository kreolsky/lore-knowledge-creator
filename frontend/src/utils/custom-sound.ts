/** The user's own notification sound: one audio file stored as a data URL in the reserved `_sound` prefs row. */
import { apiClient } from '../api/client';
import { registerEditorHostReset } from '../components/editor/editor-host';

// WHY its own prefs row, not a `_global` field: the global blob is re-PUT on every
// resize/theme change, and a few hundred KB of audio must not ride along each time.
const SOUND_PREFS_KEY = '_sound';
export const CUSTOM_SOUND_MAX_BYTES = 500 * 1024;

export interface CustomSound { name: string; dataUrl: string }
export type CustomSoundError = 'notAudio' | 'tooLarge' | 'readFailed';

let cache: CustomSound | null | undefined;
let inflight: Promise<CustomSound | null> | null = null;
registerEditorHostReset('user', () => { cache = undefined; inflight = null; });

function parse(blob: unknown): CustomSound | null {
  const b = blob as Partial<CustomSound> | null;
  return typeof b?.name === 'string' && typeof b.dataUrl === 'string' && b.dataUrl.startsWith('data:audio/')
    ? { name: b.name, dataUrl: b.dataUrl } : null;
}

/** The cached data URL, or null when not loaded yet / none stored. */
export function customSoundUrl(): string | null {
  return cache?.dataUrl ?? null;
}

/** Fetch the stored sound once; concurrent callers share the request. Rejects on a failed GET. */
export function loadCustomSound(): Promise<CustomSound | null> {
  if (cache !== undefined) return Promise.resolve(cache);
  if (!inflight) {
    inflight = apiClient.get(`/preferences/${SOUND_PREFS_KEY}`)
      .then(blob => { cache = parse(blob); return cache; })
      .finally(() => { inflight = null; });
  }
  return inflight;
}

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/** Validate, encode and store a picked file. Returns an error code for a refused file; rejects on a failed PUT. */
export async function saveCustomSound(file: File): Promise<CustomSoundError | null> {
  if (!file.type.startsWith('audio/')) return 'notAudio';
  if (file.size > CUSTOM_SOUND_MAX_BYTES) return 'tooLarge';
  let dataUrl: string;
  try { dataUrl = await readDataUrl(file); } catch { return 'readFailed'; }
  const sound = { name: file.name, dataUrl };
  await apiClient.put(`/preferences/${SOUND_PREFS_KEY}`, { preferences: sound });
  cache = sound;
  return null;
}

/** Drop the stored sound. Rejects on a failed PUT. */
export async function removeCustomSound(): Promise<void> {
  await apiClient.put(`/preferences/${SOUND_PREFS_KEY}`, { preferences: {} });
  cache = null;
}
