// Chat-only colour roles. Sticky-note colours in the application are unchanged.
export const chatPalettes = [
  {
    id: 'lavender', name: 'Лаванда', number: '01',
    note: 'Близко к нынешнему фиолетовому акценту. Мягкий сиреневый оттенок объединяет все зоны чата.',
    accent: '#B5A3FF', onAccent: '#211B36', tint: '#2D283C',
    input: '#292633', bubble: '#39314B', edge: '#605173', ink: '#D8CBF1',
  },
  {
    id: 'sage', name: 'Шалфей', number: '02',
    note: 'Приглушённый зелёный на графите. Спокойный вариант для долгой работы с текстом.',
    accent: '#9DC5AD', onAccent: '#15251C', tint: '#27332D',
    input: '#262E29', bubble: '#314338', edge: '#526F5C', ink: '#C4DEC9',
  },
  {
    id: 'ice', name: 'Ледяной синий', number: '03',
    note: 'Холодный голубой акцент. Чётко разделяет действия, контекст нового чата и сообщения.',
    accent: '#98BFEA', onAccent: '#152234', tint: '#27313E',
    input: '#262D35', bubble: '#303F51', edge: '#516984', ink: '#CCDEF1',
  },
  {
    id: 'amber', name: 'Янтарь', number: '04',
    note: 'Тёплый песочный акцент. Развивает исходный жёлтый цвет, убирая сочетание с фиолетовыми кнопками.',
    accent: '#D8B479', onAccent: '#2A2114', tint: '#352F25',
    input: '#2E2A24', bubble: '#44392A', edge: '#756245', ink: '#E5D2AC',
  },
];

export function chatPaletteCSS() {
  return chatPalettes.map(p => `[data-chat-palette="${p.id}"] {\n${Object.entries(p)
    .filter(([key]) => !['id', 'name', 'number', 'note'].includes(key))
    .map(([key, value]) => `  --chat-${key.replace(/[A-Z]/g, c => '-' + c.toLowerCase())}: ${value};`)
    .join('\n')}\n}`).join('\n\n');
}
