// Opt-in skins for Lore's existing token names. No document-content migration.
export const families = [
  {
    id: 'porcelain', number: '01', name: 'Porcelain / Graphite',
    description: 'Нейтральные поверхности и фирменный фиолетовый. Самое близкое развитие исходного Lore.',
    light: { name: 'Porcelain', note: 'Светлый холст, прохладные серые панели, чёткая иерархия.',
      bg: '#FAFAFC', surface: '#F0F0F5', surface2: '#E6E6EE', surface3: '#DDDCE8',
      'header-bg': '#E9E9F0', border: '#D2D2DF', 'border-soft': '#E5E5ED',
      text: '#252532', 'text-muted': '#5F6071', 'text-dim': '#696A7C',
      accent: '#6255D4', 'on-accent': '#FFFFFF', 'accent-soft': '#E7E3FA',
      'sticky-yellow-light': '#F7F0D4', 'sticky-yellow-dark': '#EFE1AE', 'sticky-yellow-mid': '#F2E8C0', 'sticky-yellow-sep': '#DDD0A1', 'sticky-yellow-ink': '#6B571D' },
    dark: { name: 'Graphite', note: 'Графитовый холст, приподнятые панели, мягкий лавандовый акцент.',
      bg: '#19191F', surface: '#22222B', surface2: '#2B2B37', surface3: '#373645',
      'header-bg': '#2A2935', border: '#424151', 'border-soft': '#30303C',
      text: '#ECEBF3', 'text-muted': '#B4B2C5', 'text-dim': '#9B98AE',
      accent: '#AA98F5', 'on-accent': '#211A3D', 'accent-soft': '#38304E',
      'sticky-yellow-light': '#302D24', 'sticky-yellow-dark': '#45402B', 'sticky-yellow-mid': '#3A3528', 'sticky-yellow-sep': '#504832', 'sticky-yellow-ink': '#E1D19D' },
  },
  {
    id: 'paper', number: '02', name: 'Paper / Walnut',
    description: 'Тёплая бумага и ореховый графит. Приглушённый сливовый акцент сохраняет характер Lore.',
    light: { name: 'Paper', note: 'Кремовый документ и песочные панели для длинных текстов.',
      bg: '#FAF6ED', surface: '#EFE9DF', surface2: '#E5DDD0', surface3: '#DAD0C0',
      'header-bg': '#E8E0D3', border: '#D0C5B4', 'border-soft': '#E4DCCF',
      text: '#332E29', 'text-muted': '#6C6055', 'text-dim': '#716558',
      accent: '#795675', 'on-accent': '#FFFFFF', 'accent-soft': '#E9DDE6',
      'sticky-yellow-light': '#F3E8C8', 'sticky-yellow-dark': '#EBDDAD', 'sticky-yellow-mid': '#EFE2BB', 'sticky-yellow-sep': '#D6C79E', 'sticky-yellow-ink': '#6C531C' },
    dark: { name: 'Walnut', note: 'Тёплые тёмные поверхности без чистого чёрного; мягкий сливовый акцент.',
      bg: '#211E1C', surface: '#2B2724', surface2: '#342F2B', surface3: '#443D37',
      'header-bg': '#332D29', border: '#51463E', 'border-soft': '#3A332E',
      text: '#F0E9DF', 'text-muted': '#C0B2A3', 'text-dim': '#AB9C8D',
      accent: '#C5A0BC', 'on-accent': '#2A1B26', 'accent-soft': '#45343F',
      'sticky-yellow-light': '#353023', 'sticky-yellow-dark': '#4A4028', 'sticky-yellow-mid': '#3E3625', 'sticky-yellow-sep': '#574A30', 'sticky-yellow-ink': '#E9D29C' },
  },
  {
    id: 'glacier', number: '03', name: 'Glacier / Midnight',
    description: 'Холодные серо-синие поверхности и глубокий синий акцент. Более техническое настроение.',
    light: { name: 'Glacier', note: 'Почти белый документ, голубоватые панели и синий акцент.',
      bg: '#F7FAFD', surface: '#EAF0F6', surface2: '#DDE7F0', surface3: '#CEDDEA',
      'header-bg': '#E1EAF3', border: '#C5D4E3', 'border-soft': '#DCE6EF',
      text: '#243447', 'text-muted': '#52677D', 'text-dim': '#596E82',
      accent: '#34639E', 'on-accent': '#FFFFFF', 'accent-soft': '#DCE8F8',
      'sticky-yellow-light': '#F3F0D9', 'sticky-yellow-dark': '#E9E2B8', 'sticky-yellow-mid': '#EEEBCC', 'sticky-yellow-sep': '#D5CFAB', 'sticky-yellow-ink': '#645923' },
    dark: { name: 'Midnight', note: 'Чернильный документ, синие панели и светлый голубой акцент.',
      bg: '#141F2D', surface: '#1B2A3C', surface2: '#24354A', surface3: '#30465E',
      'header-bg': '#213247', border: '#3B526B', 'border-soft': '#2B3E54',
      text: '#E6EDF6', 'text-muted': '#B0C0D3', 'text-dim': '#98ACC2',
      accent: '#94BDED', 'on-accent': '#172A40', 'accent-soft': '#2A4160',
      'sticky-yellow-light': '#2E302A', 'sticky-yellow-dark': '#414330', 'sticky-yellow-mid': '#383A2D', 'sticky-yellow-sep': '#4B4E39', 'sticky-yellow-ink': '#DFD6AA' },
  },
];

export const highlights = {
  light: [['#FFE0C2', '#68370D'], ['#DDEAFF', '#294C7D'], ['#E0ECC8', '#3C531F'], ['#EBDDFA', '#573781'], ['#FFF0B8', '#685014']],
  dark: [['#563820', '#FFDCB6'], ['#283F61', '#CDE1FF'], ['#354829', '#DEEDBC'], ['#49365D', '#EDDAFF'], ['#514623', '#F8E8AE']],
};
export const storedHighlights = ['#ec883c', '#8ab4ff', '#8ab440', '#a978d6', '#fdd663'];

export function tokens(family, mode) {
  const { name, note, ...base } = family[mode];
  const dark = mode === 'dark';
  return {
    ...base,
    'surface-transclusion': `color-mix(in srgb, ${base.bg} 50%, ${base.surface})`,
    'composer-control-bg': dark ? base.bg : base.surface2,
    'preview-title-bg': dark ? base.bg : base.surface2,
    'accent-glow': `color-mix(in srgb, ${base.accent} 25%, transparent)`,
    'handle-bg': base.border, 'handle-bg-hover': base.accent,
    green: dark ? '#9DD6AB' : '#26763D', amber: dark ? '#E9C579' : '#8C630D',
    red: dark ? '#F4A4A4' : '#B03737', blue: dark ? '#ACCCF5' : '#3568A4', orange: dark ? '#F4BF8F' : '#9D571C',
    'sticky-blue-light': dark ? '#263D56' : '#DDEAF8', 'sticky-blue-dark': dark ? '#304D6D' : '#CCDEF2', 'sticky-blue-darkest': dark ? '#567FAE' : '#496E9C',
    'sticky-green-light': dark ? '#2B4236' : '#DEEDD9', 'sticky-green-dark': dark ? '#385644' : '#CCDFCB', 'sticky-green-darkest': dark ? '#5E9475' : '#477956',
    'sticky-purple-light': dark ? '#3A304F' : '#E9E0F6',
    ...Object.fromEntries(highlights[mode].flatMap(([bg, ink], index) => [[`color-highlight-${index + 1}`, bg], [`highlight-ink-${index + 1}`, ink]])),
    // Agent action is a filled button with white text; distinct from text-marker fill.
    'color-highlight-agent': '#9B561E',
  };
}

export function makeCSS() {
  let css = '/* Lore theme variants. Load after src/index.css. Opt in with data-lore-palette. */\n';
  for (const family of families) for (const mode of ['light', 'dark']) {
    css += `\nhtml[data-lore-palette="${family.id}"]${mode === 'dark' ? '.dark' : ':not(.dark)'} {\n`;
    css += Object.entries(tokens(family, mode)).map(([k, v]) => `  --${k}: ${v};`).join('\n') + '\n}\n';
  }
  css += '\n/* Existing Button/IconButton/brand logo use white text; dark accents need dark ink. */\n';
  css += 'html[data-lore-palette] .bg-accent.text-white { color: var(--on-accent); }\n';
  css += '\n/* Match the exact inline style emitted by build-structural.ts (standard palette only).\n   Persisted hex markup and custom document colours stay unchanged. */\n';
  storedHighlights.forEach((hex, i) => {
    css += `html[data-lore-palette] .cm-color-swatch:is([style="background-color:${hex}" i], [style="background-color:${hex};" i]) { background-color: var(--color-highlight-${i + 1}) !important; color: var(--highlight-ink-${i + 1}); }\n`;
  });
  return css;
}
