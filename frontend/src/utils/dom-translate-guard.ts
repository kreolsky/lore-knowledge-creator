/** Keeps React alive when a browser translator (Google Translate) has rewritten its DOM. */
// WHY: the translator swaps React-owned text nodes for its own <font> wrappers, so React's
// next removeChild/insertBefore names a node that is no longer a child and throws
// "NotFoundError", taking the whole app down through the ErrorBoundary. This is React
// issue #11538 (open since 2017); the guard below is the workaround suggested there.
// Cost: a text node the translator replaced stops updating — visible only to someone who
// turned translation on, whose page text is already not ours.

import { sendTelemetry } from '../telemetry/telemetry';

let reported = false;

function report(op: 'removeChild' | 'insertBefore'): void {
  console.warn(`[dom-translate-guard] ${op} on a node moved by a translator — skipped`);
  // One row per tab: a translated page trips this on every re-render.
  if (reported) return;
  reported = true;
  sendTelemetry({ category: 'client', kind: 'dom-mismatch', detail: { op } });
}

export function installDomTranslateGuard(): void {
  if (typeof Node !== 'function' || !Node.prototype) return;

  const originalRemoveChild = Node.prototype.removeChild;
  Node.prototype.removeChild = function <T extends Node>(this: Node, child: T): T {
    if (child.parentNode !== this) {
      report('removeChild');
      return child;
    }
    return originalRemoveChild.call(this, child) as T;
  };

  const originalInsertBefore = Node.prototype.insertBefore;
  Node.prototype.insertBefore = function <T extends Node>(this: Node, newNode: T, referenceNode: Node | null): T {
    if (referenceNode && referenceNode.parentNode !== this) {
      report('insertBefore');
      return newNode;
    }
    return originalInsertBefore.call(this, newNode, referenceNode) as T;
  };
}
