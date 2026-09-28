// @vitest-environment jsdom
/** The presence bar is a line decoration anchored to the text line (not a gutter). */
import { describe, it, expect } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import { remotePresenceField, setPresence } from './presence-state';
import { presenceDecorations } from './presence-decorations';

describe('presenceDecorations', () => {
  it('places a line decoration at the start of each line with presence', () => {
    const view = new EditorView({
      state: EditorState.create({
        doc: 'line one\nline two\nline three',
        extensions: [remotePresenceField, presenceDecorations],
      }),
      parent: document.body,
    });

    view.dispatch({
      effects: setPresence.of(new Map([[3, [{ id: 'p', name: 'Peer', color: '#abcdef' }]]])),
    });

    const set = view.plugin(presenceDecorations)!.decorations;
    const froms: number[] = [];
    set.between(0, view.state.doc.length, from => { froms.push(from); });
    expect(froms).toEqual([view.state.doc.line(3).from]);

    view.destroy();
  });

  it('drops decorations when presence clears', () => {
    const view = new EditorView({
      state: EditorState.create({
        doc: 'a\nb',
        extensions: [remotePresenceField, presenceDecorations],
      }),
      parent: document.body,
    });
    view.dispatch({ effects: setPresence.of(new Map([[1, [{ id: 'p', name: 'P', color: '#fff' }]]])) });
    expect(view.plugin(presenceDecorations)!.decorations.size).toBe(1);
    view.dispatch({ effects: setPresence.of(new Map()) });
    expect(view.plugin(presenceDecorations)!.decorations.size).toBe(0);
    view.destroy();
  });
});
