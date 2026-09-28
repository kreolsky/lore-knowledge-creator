/**
 * Remote-presence bars rendered as line decorations anchored to the text column.
 *
 * // WHY: a CM6 gutter sits at the scroller's far-left edge; with a centered, max-width
 * // content column that leaves the bar stranded far from the text on wide monitors.
 * // Line decorations live inside .cm-content, so the bar hugs the text at any width.
 */

import { Decoration, EditorView, ViewPlugin, type ViewUpdate, type DecorationSet } from '@codemirror/view';
import { RangeSetBuilder } from '@codemirror/state';
import { remotePresenceField, type RemoteUser } from './presence-state';

// INVARIANT: bar color === chip color === userColor(user_id). Why: the user matches
// the margin bar to the name tag at a glance. (Color arrives in awareness already
// computed by the peer via userColor.) Multiple users on one line split the bar.
function presenceBackground(users: RemoteUser[]): string {
  if (users.length === 1) return users[0].color;
  const step = 100 / users.length;
  const stops = users.map((u, i) => `${u.color} ${i * step}% ${(i + 1) * step}%`).join(', ');
  return `linear-gradient(to bottom, ${stops})`;
}

function lineDecoration(users: RemoteUser[]): Decoration {
  return Decoration.line({
    class: 'cm-presence-line',
    attributes: {
      style: `--cm-presence-bg: ${presenceBackground(users)}`,
      title: users.map(u => u.name).join(', '),
    },
  });
}

function build(view: EditorView): DecorationSet {
  const map = view.state.field(remotePresenceField);
  const builder = new RangeSetBuilder<Decoration>();
  const total = view.state.doc.lines;
  for (const lineNo of [...map.keys()].sort((a, b) => a - b)) {
    if (lineNo < 1 || lineNo > total) continue;
    const users = map.get(lineNo)!;
    if (users.length === 0) continue;
    const from = view.state.doc.line(lineNo).from;
    builder.add(from, from, lineDecoration(users));
  }
  return builder.finish();
}

export const presenceDecorations = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;

    constructor(view: EditorView) {
      this.decorations = build(view);
    }

    update(update: ViewUpdate) {
      if (
        update.docChanged ||
        update.startState.field(remotePresenceField) !== update.state.field(remotePresenceField)
      ) {
        this.decorations = build(update.view);
      }
    }
  },
  { decorations: v => v.decorations },
);
