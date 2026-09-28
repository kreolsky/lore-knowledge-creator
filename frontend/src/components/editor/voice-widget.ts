/**
 * CM6 inline voice recording widget — appears at cursor on Cmd+D.
 *
 * Three states: recording (red mic + timer + stop) → recognizing (spinner) → removed (text inserted).
 * Uses StateField because lifecycle is driven by external events, not document content.
 * Visually identical to the header REC button — shares .header-recording-btn CSS.
 *
 * Voice widget lifecycle (Cmd+D shortcut flow):
 *   1. First Cmd+D → voiceWidgetStart → widget appears at cursor (recording, red)
 *   2. Second Cmd+D → voiceWidgetRecognizing → widget transitions to amber spinner
 *   3. Transcription complete → voiceWidgetRemove → widget replaced by transcribed text
 *   4. Escape at any time → voiceWidgetRemove + cancel flag → widget removed, no text
 *
 * Hover behavior:
 *   - recording state: clickable (mousedown stops recording), hover has no visual change
 *   - recognizing state: non-interactive (pointer-events: none via CSS), no hover effects
 */
// ARCH: StateField (not ViewPlugin) — widget lifecycle is external (Cmd+D toggle, STT completion), same rationale as tableRenderField.
// SYSTEM: voice-widget — CM6 inline voice recording widget (StateField, Cmd+D toggle)

import { type Extension, Prec, StateEffect, StateField } from '@codemirror/state';
import { Decoration, EditorView, keymap, WidgetType } from '@codemirror/view';
import { emit } from '../../events';
import { setVoiceCancelled } from './voice-cancel-flag';

// Pre-parsed SVG templates — cloneNode is ~10x faster than innerHTML per toDOM() call
const _tpl = document.createElement('template');
_tpl.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>`;
const MIC_NODE = _tpl.content.firstChild as SVGElement;
_tpl.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/></svg>`;
const SQUARE_NODE = _tpl.content.firstChild as SVGElement;

// ── State Effects ──

export const voiceWidgetStart = StateEffect.define<{ pos: number; startedAt: number }>();
export const voiceWidgetRecognizing = StateEffect.define<void>();
export const voiceWidgetRemove = StateEffect.define<void>();

// ── Widget Types ──

type VoiceState = 'recording' | 'recognizing';

class VoiceRecordingWidget extends WidgetType {
  private timerId: ReturnType<typeof setInterval> | null = null;
  private btn: HTMLElement | null = null;
  private mousedownHandler: ((e: MouseEvent) => void) | null = null;

  constructor(
    readonly voiceState: VoiceState,
    readonly startedAt: number,
  ) { super(); }

  toDOM(): HTMLElement {
    const btn = document.createElement('span');
    this.btn = btn;
    btn.title = this.voiceState === 'recording'
      ? 'Recording… (Cmd+D to stop, Esc to cancel)'
      : 'Recognizing…';

    if (this.voiceState === 'recording') {
      btn.className = 'header-recording-btn cm-voice-widget';
      btn.appendChild(MIC_NODE.cloneNode(true));
      const label = document.createElement('span');
      label.className = 'recording-label';
      label.textContent = '0:00';
      btn.appendChild(label);
      btn.appendChild(SQUARE_NODE.cloneNode(true));
      const update = () => {
        const sec = Math.floor((Date.now() - this.startedAt) / 1000);
        const m = Math.floor(sec / 60);
        const s = sec % 60;
        label.textContent = `${m}:${s.toString().padStart(2, '0')}`;
      };
      update();
      this.timerId = setInterval(update, 1000);

      this.mousedownHandler = (e: MouseEvent) => {
        e.preventDefault();
        e.stopPropagation();
        const view = EditorView.findFromDOM(btn);
        if (view) view.dispatch({ effects: voiceWidgetRecognizing.of(undefined) });
        emit('stop-recording');
      };
      btn.addEventListener('mousedown', this.mousedownHandler);
    } else {
      btn.className = 'header-recording-btn header-recording-btn--recognizing cm-voice-widget';
    }

    return btn;
  }

  destroy(): void {
    if (this.timerId) clearInterval(this.timerId);
    if (this.btn && this.mousedownHandler) {
      this.btn.removeEventListener('mousedown', this.mousedownHandler);
    }
  }

  eq(other: VoiceRecordingWidget): boolean {
    return this.voiceState === other.voiceState && this.startedAt === other.startedAt;
  }

  ignoreEvent(): boolean {
    return true;
  }
}

// ── StateField ──

interface VoiceWidgetState {
  pos: number;
  state: VoiceState;
  startedAt: number;
}

export const voiceWidgetField: StateField<VoiceWidgetState | null> = StateField.define<VoiceWidgetState | null>({
  create: () => null,

  update(value, tr) {
    for (const e of tr.effects) {
      if (e.is(voiceWidgetStart)) {
        return { pos: e.value.pos, state: 'recording', startedAt: e.value.startedAt };
      }
      if (e.is(voiceWidgetRecognizing) && value) {
        return { ...value, state: 'recognizing' };
      }
      if (e.is(voiceWidgetRemove)) {
        return null;
      }
    }
    // Adjust position on doc changes
    if (value && tr.docChanged) {
      const newPos = tr.changes.mapPos(value.pos);
      return { ...value, pos: newPos };
    }
    return value;
  },

  provide(field) {
    return EditorView.decorations.from(field, (value) => {
      if (!value) return Decoration.none;
      const widget = new VoiceRecordingWidget(value.state, value.startedAt);
      return Decoration.set([
        Decoration.widget({ widget, side: 1 }).range(value.pos),
      ]);
    });
  },
});

// ── Escape keymap — only fires when voice widget is active ──

const voiceEscapeKeymap = Prec.high(keymap.of([{
  key: 'Escape',
  run(view) {
    const widgetState = view.state.field(voiceWidgetField);
    if (!widgetState) return false;
    // Cancel recording without saving — set flag before stop so useVoiceInput discards the file
    setVoiceCancelled(true);
    emit('stop-recording');
    view.dispatch({ effects: voiceWidgetRemove.of(undefined) });
    return true;
  },
}]));

// ── Bundled extension ──

export const voiceWidgetExtension: Extension = [
  voiceWidgetField,
  voiceEscapeKeymap,
];
