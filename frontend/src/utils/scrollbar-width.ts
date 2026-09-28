/** Publishes the platform's real horizontal-scrollbar height as --dsh-scrollbar-width. */

// INVARIANT: --dsh-scrollbar-width equals the layout height the browser actually gives
// a horizontal scrollbar here (0 for macOS overlay bars), measured, never assumed.
// Why: the dsh chat renderer reserves that much padding under a wide table and swaps it
// for the bar on hover. With the static 8px, an overlay bar took no space, the 8px
// vanished on hover and the text under the table jumped up.
export function publishScrollbarWidth(): void {
  const probe = document.createElement('div');
  probe.style.cssText = 'position:absolute;top:-9999px;width:100px;overflow-x:scroll;visibility:hidden';
  const inner = document.createElement('div');
  inner.style.cssText = 'width:200px;height:1px';
  probe.appendChild(inner);
  document.body.appendChild(probe);
  const height = probe.offsetHeight - probe.clientHeight;
  probe.remove();
  document.documentElement.style.setProperty('--dsh-scrollbar-width', `${height}px`);
}
