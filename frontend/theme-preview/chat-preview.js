(() => {
  const buttons = [...document.querySelectorAll('[data-pick]')];
  const views = [...document.querySelectorAll('[data-chat-palette]')];
  const valid = new Set(buttons.map(button => button.dataset.pick));
  function show(selection, updateURL = false) {
    const value = valid.has(selection) ? selection : 'lavender';
    for (const view of views) view.hidden = value !== 'all' && view.dataset.chatPalette !== value;
    for (const button of buttons) button.setAttribute('aria-pressed', String(button.dataset.pick === value));
    if (updateURL) {
      const url = new URL(location.href);
      url.searchParams.set('chat', value);
      // Local file URLs can restrict History API updates. Switching still works.
      try { history.replaceState(null, '', url); } catch { /* No server required. */ }
    }
  }
  for (const button of buttons) button.addEventListener('click', () => show(button.dataset.pick, true));
  show(new URLSearchParams(location.search).get('chat'));
  window.addEventListener('popstate', () => show(new URLSearchParams(location.search).get('chat')));
})();
