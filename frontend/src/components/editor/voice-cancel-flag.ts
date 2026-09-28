/** Module-level flag — set by Escape in voice-widget, consumed by useVoiceInput. */
let _voiceCancelled = false;

export function setVoiceCancelled(v: boolean): void {
  _voiceCancelled = v;
}

/**
 * Non-consuming read — lets useAudioRecorder's onstop drop the cached take on
 * Escape without stealing the ONE consuming read that belongs to useVoiceInput
 * (a second consuming read would resurrect the widget sync-upload of a
 * discarded take).
 */
export function peekVoiceCancelled(): boolean {
  return _voiceCancelled;
}

export function isVoiceCancelled(): boolean {
  const v = _voiceCancelled;
  _voiceCancelled = false;
  return v;
}
