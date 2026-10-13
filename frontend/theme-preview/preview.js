(() => {
  const data = JSON.parse(document.getElementById('palette-data').textContent);
  const params = new URLSearchParams(location.search);
  let family = data.find(f => f.id === params.get('palette')) || data[0];
  let mode = params.get('mode') === 'dark' ? 'dark' : 'light';
  let original = params.get('original') === '1';
  if (params.get('export') === '1') document.documentElement.dataset.export = 'true';
  const labels = [['bg','Документ'], ['surface','Панели'], ['header-bg','Верхняя панель'], ['border','Разделители'], ['text','Текст'], ['text-muted','Вторичный текст'], ['accent','Акцент'], ['sticky-yellow-light','Контекст']];
  function update() {
    const root = document.documentElement;
    root.classList.toggle('dark', mode === 'dark');
    root.style.colorScheme = mode;
    if (original) delete root.dataset.lorePalette;
    else root.dataset.lorePalette = family.id;
    document.querySelectorAll('[data-family]').forEach(button => button.setAttribute('aria-pressed', String(!original && button.dataset.family === family.id)));
    document.querySelectorAll('[data-mode]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mode === mode)));
    document.querySelector('.original-toggle').setAttribute('aria-pressed', String(original));
    document.getElementById('variant-name').textContent = original ? 'Lore · оригинал' : family[mode].name;
    document.getElementById('variant-number').textContent = `${original ? '00' : family.number} / ${mode.toUpperCase()}`;
    document.getElementById('variant-note').textContent = original ? 'Текущие значения из frontend/src/index.css' : family[mode].note;
    document.getElementById('family-description').textContent = original ? 'Исходные CSS-переменные, геометрия и цвета маркеров из репозитория.' : family.description;
    const style = getComputedStyle(root);
    const swatches = document.getElementById('swatches');
    swatches.replaceChildren();
    for (const [key, label] of labels) {
      const value = style.getPropertyValue(`--${key}`).trim();
      const swatch = document.createElement('div');
      const color = document.createElement('div'); color.className = 'swatch-color'; color.style.backgroundColor = value;
      const name = document.createElement('div'); name.className = 'swatch-title'; name.textContent = label;
      const hex = document.createElement('div'); hex.className = 'swatch-value'; hex.textContent = value.toUpperCase();
      swatch.append(color, name, hex); swatches.append(swatch);
    }
    const next = new URL(location.href); next.searchParams.set('palette', family.id); next.searchParams.set('mode', mode);
    if (original) next.searchParams.set('original', '1'); else next.searchParams.delete('original');
    try { history.replaceState(null, '', next); } catch { /* file:// still supports every preview control. */ }
  }
  document.querySelectorAll('[data-family]').forEach(button => button.addEventListener('click', () => { family = data.find(f => f.id === button.dataset.family); original = false; update(); }));
  document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => { mode = button.dataset.mode; update(); }));
  document.querySelector('.original-toggle').addEventListener('click', () => { original = !original; update(); });
  const textarea = document.querySelector('textarea'); textarea.id = 'preview-message';
  textarea.setAttribute('aria-label', 'Сообщение — поле предпросмотра, без отправки');
  update();
})();
