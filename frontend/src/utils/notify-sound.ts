/** The agent-notification player: the user's sound, gated by this project's sound toggle. */
import { useUIStore } from '../store/ui-store';
import { playSoundPreset } from './sound-presets';

/** Play the user's chosen sound when this project's sound toggle is on. */
export function playNotifySound(): void {
  const { soundEnabled, soundPreset } = useUIStore.getState();
  if (!soundEnabled) return;
  playSoundPreset(soundPreset);
}
